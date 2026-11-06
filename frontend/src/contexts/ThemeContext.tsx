import React, { createContext, useContext, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
    ThemeId, ThemeVars, ThemeVarKey, ThemeDef, ThemeScene, SceneKey, ThemeScheme, BuiltinThemeId, THEME_CSS_VARS,
    THEME_VAR_KEYS, getTheme, DEFAULT_THEME_ID, isUserThemeId, isBuiltinThemeId, themeCssVars, sceneCssVars,
    sceneGround,
} from '@/lib/themes';
import {
    UserTheme, FONT_ROLE_VARS, newUserTheme, copyUserTheme, normalizeUserThemes, uniqueLabel, userThemeToDef,
} from '@/lib/userThemes';
import { FontRole, stackFor } from '@/lib/fonts';
import { putChannelSettings } from '@/lib/api';
import { readBootCache, writeBootCacheSection } from '@/lib/settingsCache';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { desktopBridge } from '@/lib/desktop';

// Themes are PER CHANNEL — the `theme` section of the channel settings doc
// ({ activeThemeId, userThemes }). The provider adopts the section whenever
// useChannelSettingsStore loads a channel's doc (switching channels switches
// the whole look) — keyed on the section itself, so a patch of another
// section (the stack width default) never re-adopts the load-time theme —
// and persists local edits back with a debounced, section-wholesale PUT.
// The localStorage boot cache paints the last-active channel's theme before
// the first settings load, so boot never flashes. Both are stored data, so
// every user skin read from them is normalised (lib/userThemes) on the way in.

interface ThemeContextValue {
    themeId: ThemeId;
    effectiveVars: ThemeVars;
    /** The active skin's scene (the host-only ground, pattern and edge). */
    scene: ThemeScene;
    userThemes: UserTheme[];
    setThemeId: (id: ThemeId) => void;
    // ── User skins (the theme editor) ─────────────────────────────
    /** The "+" card: a new user skin copying the active one (a built-in
     *  becomes its base, with no overrides), selected; returns its id. */
    createUserTheme: () => string;
    renameUserTheme: (id: string, label: string) => void;
    /** Delete a user skin; if it was active, its base becomes active. */
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

/** The channel whose theme is on show, whose body store holds the scene
 *  picture: the one whose settings doc was adopted, else the active channel
 *  ('' when none). */
export function useThemeChannel(): string {
    const adopted = useChannelSettingsStore(s => s.loadedFor);
    const active = useCollimateStore(s => s.activeChannel?.id ?? '');
    return adopted || active;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/** Write every token on <html> under its CSS name, the scene's host-only
 *  properties, the scheme and the skin id. Tailwind's utilities resolve the
 *  tokens through app/globals.css; SceneBackground and the selection edge
 *  read the scene's. */
function applyTheme(themeId: ThemeId, vars: ThemeVars, scene: ThemeScene, scheme: ThemeScheme, channel: string) {
    const root = document.documentElement;
    for (const key of THEME_VAR_KEYS) root.style.setProperty(THEME_CSS_VARS[key], vars[key]);
    for (const [name, value] of Object.entries(sceneCssVars(scene, vars, channel))) root.style.setProperty(name, value);
    root.style.setProperty('color-scheme', scheme);
    root.setAttribute('data-theme', themeId);
    root.setAttribute('data-scheme', scheme);
}

/** A stored id is only trustworthy if it resolves: a built-in, or a user
 *  theme present in the given library. Anything else → default. */
function validThemeId(id: string | null | undefined, library: UserTheme[]): ThemeId {
    if (id && isBuiltinThemeId(id)) return id;
    if (id && isUserThemeId(id) && library.some(u => u.id === id)) return id;
    return DEFAULT_THEME_ID;
}

// Persist debounce: skin edits arrive per color-input tick, so both the
// change comparison and the PUT are debounced behind one timer.
const PERSIST_DEBOUNCE_MS = 400;

export function ThemeProvider({ children }: { children: React.ReactNode }) {
    // Initial paint = boot cache (last-active channel's theme) — the real
    // channel doc adopts over it as soon as useChannelSettingsStore loads.
    const [{ themeId, userThemes }, setThemeState] = useState<{
        themeId: ThemeId; userThemes: UserTheme[];
    }>(() => {
        const cached = typeof window === 'undefined' ? undefined : readBootCache().theme;
        const library = normalizeUserThemes(cached?.userThemes);
        return { themeId: validThemeId(cached?.activeThemeId, library), userThemes: library };
    });
    const setThemeId = useCallback((id: ThemeId) => {
        setThemeState(prev => ({ ...prev, themeId: id }));
    }, []);
    const setUserThemes = useCallback((fn: (prev: UserTheme[]) => UserTheme[]) => {
        setThemeState(prev => ({ ...prev, userThemes: fn(prev.userThemes) }));
    }, []);

    // JSON of the last adopted-or-persisted theme section. The persist effect
    // compares against it so adopting a server doc never echoes a PUT back.
    const lastSyncedRef = useRef<string>('');
    // The persist effect's armed-but-unfired save: the channel captured at
    // edit time and the section it would PUT. The adopt effect flushes it.
    const pendingRef = useRef<{
        channel: string | null;
        section: { activeThemeId: ThemeId; userThemes: UserTheme[] };
        timer: ReturnType<typeof setTimeout>;
    } | null>(null);

    // ── Adopt the active channel's theme section ──────────────────
    // The doc is a load-time snapshot (useChannelSettingsStore): only a new
    // theme section — a load or a forced reload — is something to adopt.
    const themeSection = useChannelSettingsStore(s => s.doc?.theme ?? null);
    useEffect(() => {
        if (!themeSection) return;
        // An edit still inside the persist debounce belongs to the channel it
        // was made in. Adopting another channel's doc re-renders with new
        // state and the persist cleanup would cancel that timer, so PUT it
        // now. Same channel (forced reload after an external write): the
        // server doc wins. No channel (boot, a switch still loading): cache
        // only, nothing to PUT.
        const pending = pendingRef.current;
        if (pending) {
            clearTimeout(pending.timer);
            pendingRef.current = null;
            const newChannel = useChannelSettingsStore.getState().loadedFor;
            if (pending.channel && pending.channel !== newChannel
                && JSON.stringify(pending.section) !== lastSyncedRef.current) {
                void putChannelSettings(pending.channel, { theme: pending.section }).catch(() => {});
            }
        }
        const library = normalizeUserThemes(themeSection.userThemes);
        const next = {
            themeId: validThemeId(themeSection.activeThemeId, library),
            userThemes: library,
        };
        lastSyncedRef.current = JSON.stringify({
            activeThemeId: next.themeId, userThemes: next.userThemes,
        });
        // Suppress CSS transitions for one frame so the channel switch is a
        // clean swap, not a cross-fade of every themed token.
        document.documentElement.setAttribute('data-theme-swapping', '');
        requestAnimationFrame(() => {
            document.documentElement.removeAttribute('data-theme-swapping');
        });
        setThemeState(next);
    }, [themeSection]);

    // ── Persist local edits (debounced, section-wholesale) ────────
    // The PUT targets the channel whose doc this theme state was ADOPTED
    // from (loadedFor), captured at edit time — never the live active
    // channel, which may already have switched by the time the debounce
    // fires (that gap would write channel A's theme into channel B). A save
    // still pending when another channel's doc is adopted is flushed to its
    // captured channel by the adopt effect above.
    useEffect(() => {
        if (typeof window === 'undefined') return;
        const channel = useChannelSettingsStore.getState().loadedFor;
        const section = { activeThemeId: themeId, userThemes };
        const timer = setTimeout(() => {
            if (pendingRef.current?.timer === timer) pendingRef.current = null;
            const json = JSON.stringify(section);
            if (json === lastSyncedRef.current) return;  // adopted or already saved
            lastSyncedRef.current = json;
            writeBootCacheSection('theme', section);
            if (!channel) return;  // create view, boot, a switch still loading — cache only
            void putChannelSettings(channel, { theme: section }).catch(() => {
                // Best-effort: the look is already applied locally; the next
                // successful save re-syncs the doc.
            });
        }, PERSIST_DEBOUNCE_MS);
        pendingRef.current = { channel, section, timer };
        return () => {
            clearTimeout(timer);
            if (pendingRef.current?.timer === timer) pendingRef.current = null;
        };
    }, [themeId, userThemes]);

    const baseTheme = useMemo<ThemeDef>(() => {
        if (isUserThemeId(themeId)) {
            const found = userThemes.find(u => u.id === themeId);
            if (found) return userThemeToDef(found);
        }
        return getTheme(themeId);
    }, [themeId, userThemes]);

    const scheme: ThemeScheme = baseTheme.scheme;
    // User skins already carry their base's tokens and scene under their overrides.
    const effectiveVars = baseTheme.vars;
    const scene = baseTheme.scene;
    // The scene picture lives in the body store of the channel whose theme is
    // shown: the one whose settings doc was adopted, else the active channel.
    const sceneChannel = useThemeChannel();

    // Apply the skin, then tell renderers whenever the record they receive
    // changed: the id, the scheme or any token (the scene is not theirs). A
    // new user skin copying the active one changes only the id, and the
    // frames still need it (their data-theme mirrors it); nothing else is
    // re-sent.
    const dispatchedTheme = useRef<string | null>(null);
    useEffect(() => {
        if (typeof window === 'undefined') return;
        applyTheme(themeId, effectiveVars, scene, scheme, sceneChannel);
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
    }, [themeId, scheme, effectiveVars, scene, sceneChannel]);

    // The desktop window's colour is what a resize or a GPU restart exposes
    // before the page repaints: the scene's ground, which fills the window
    // below the bars (docs/ai_tools note feedback_flicker_app_side_levers).
    const windowBackground = sceneGround(scene, effectiveVars);
    const { sheet: windowOverlay, ink: windowSymbol } = effectiveVars;
    useEffect(() => {
        desktopBridge()?.setWindowTheme?.({ background: windowBackground, overlay: windowOverlay, symbol: windowSymbol });
    }, [windowBackground, windowOverlay, windowSymbol]);

    // ── User skins (the theme editor) ─────────────────────────────

    const createUserTheme = useCallback((): string => {
        const label = uniqueLabel('My theme', userThemes.map(u => u.label));
        const activeUser = userThemes.find(u => u.id === themeId);
        const created = activeUser
            ? copyUserTheme(activeUser, label)
            : newUserTheme(isBuiltinThemeId(themeId) ? themeId : DEFAULT_THEME_ID, label);
        setThemeState(prev => ({
            themeId: created.id,
            userThemes: [...prev.userThemes, created],
        }));
        return created.id;
    }, [themeId, userThemes]);

    const updateUserTheme = useCallback((id: string, fn: (u: UserTheme) => UserTheme) => {
        setUserThemes(prev => prev.map(u => u.id === id ? fn(u) : u));
    }, [setUserThemes]);

    const renameUserTheme = useCallback((id: string, label: string) => {
        updateUserTheme(id, u => ({ ...u, label }));
    }, [updateUserTheme]);

    const deleteUserTheme = useCallback((id: string) => {
        setThemeState(prev => {
            const deleted = prev.userThemes.find(u => u.id === id);
            return {
                themeId: prev.themeId === id ? deleted?.baseThemeId ?? DEFAULT_THEME_ID : prev.themeId,
                userThemes: prev.userThemes.filter(u => u.id !== id),
            };
        });
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
        themeId, effectiveVars, scene, userThemes, setThemeId,
        createUserTheme, renameUserTheme, deleteUserTheme, setUserThemeBase,
        setUserThemeScheme, setUserThemeVar, resetUserThemeVar, setUserThemeFont,
        setUserThemeScene, resetUserThemeScene,
    }), [
        themeId, effectiveVars, scene, userThemes, setThemeId,
        createUserTheme, renameUserTheme, deleteUserTheme, setUserThemeBase,
        setUserThemeScheme, setUserThemeVar, resetUserThemeVar, setUserThemeFont,
        setUserThemeScene, resetUserThemeScene,
    ]);

    return (
        <ThemeContext.Provider value={value}>
            {children}
        </ThemeContext.Provider>
    );
}

export function useThemeContext() {
    const ctx = useContext(ThemeContext);
    if (!ctx) throw new Error('useThemeContext must be used within ThemeProvider');
    return ctx;
}
