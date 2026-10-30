// User skins: a built-in base plus overrides, stored PER CHANNEL as the
// `theme.userThemes` array of the channel settings doc (and thereby in the
// settings JSON export; the on-disk shape is this object, verbatim).
// Every token a user skin does not override is its base's, except the soft
// accents: an overridden brass, teal or red brings its soft fill along,
// derived at the base's alpha, until the soft fill is overridden itself.

import {
    ThemeVars, ThemeDef, ThemeScheme, BuiltinThemeId, THEME_CSS_VARS, getTheme, isBuiltinThemeId,
    isUserThemeId, softVariant,
} from './themes';
import { FontRole } from './fonts';

export interface UserTheme {
    /** `user.<slug>`, assigned on create. */
    id: `user.${string}`;
    /** Shown on the skin card. */
    label: string;
    scheme: ThemeScheme;
    /** Overrides over the base skin's tokens. */
    vars: Partial<ThemeVars>;
    /** The built-in whose tokens fill every one not overridden. */
    baseThemeId: BuiltinThemeId;
    createdAt: number;
}

/** Each soft accent and the accent it is derived from. */
export const SOFT_ACCENTS = {
    brassSoft: 'brass',
    tealSoft: 'teal',
    redSoft: 'red',
} as const satisfies Partial<Record<keyof ThemeVars, keyof ThemeVars>>;

export type SoftAccentKey = keyof typeof SOFT_ACCENTS;

export function isSoftAccent(key: keyof ThemeVars): key is SoftAccentKey {
    return key in SOFT_ACCENTS;
}

/** The token that holds each font role's family stack. */
export const FONT_ROLE_VARS = {
    ui: 'fontUi',
    prose: 'fontProse',
    mono: 'fontMono',
} as const satisfies Record<FontRole, keyof ThemeVars>;

/** Random short id suitable for user skin ids. */
function randomSlug(n = 8): string {
    const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let out = '';
    for (let i = 0; i < n; i++) out += alphabet[Math.floor(Math.random() * alphabet.length)];
    return out;
}

/** `label`, or `label 2`, `label 3`… — the first not already taken. */
export function uniqueLabel(label: string, taken: readonly string[]): string {
    if (!taken.includes(label)) return label;
    let n = 2;
    while (taken.includes(`${label} ${n}`)) n++;
    return `${label} ${n}`;
}

/** A new user skin on `base` with no overrides: it looks exactly like its base. */
export function newUserTheme(base: BuiltinThemeId, label: string): UserTheme {
    return {
        id: `user.${randomSlug()}`,
        label,
        scheme: getTheme(base).scheme,
        vars: {},
        baseThemeId: base,
        createdAt: Date.now(),
    };
}

/** A copy of `source` under a new id: same base, scheme and overrides. */
export function copyUserTheme(source: UserTheme, label: string): UserTheme {
    return {
        ...source,
        id: `user.${randomSlug()}`,
        label,
        vars: { ...source.vars },
        createdAt: Date.now(),
    };
}

/** `color` as `#rrggbb` when it is a hex or an rgb/rgba function colour (alpha
 *  dropped), else undefined. The colour picker and soft derivation read it. */
export function hexOf(color: string): string | undefined {
    const value = color.trim();
    const long = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(value);
    if (long) return `#${long[1].toLowerCase()}`;
    const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])([0-9a-f])?$/i.exec(value);
    if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toLowerCase();
    const rgb = /^rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})\s*(?:[,/]\s*[\d.]+%?\s*)?\)$/i.exec(value);
    if (!rgb) return undefined;
    const channels = rgb.slice(1, 4).map(Number);
    if (channels.some(c => c > 255)) return undefined;
    return `#${channels.map(c => c.toString(16).padStart(2, '0')).join('')}`;
}

/** The soft fill `key` takes in `user` when it is not overridden: the
 *  overridden accent at the base's alpha, or the base's own soft fill. */
export function derivedSoft(user: UserTheme, key: SoftAccentKey): string {
    const baseSoft = getTheme(user.baseThemeId).vars[key];
    const accent = user.vars[SOFT_ACCENTS[key]];
    if (accent === undefined) return baseSoft;
    return softVariant(hexOf(accent) ?? accent, baseSoft);
}

/** The user skin as a full ThemeDef: the base's tokens under its overrides,
 *  with the soft accents derived where only their accent is overridden. */
export function userThemeToDef(user: UserTheme): ThemeDef {
    const vars: ThemeVars = { ...getTheme(user.baseThemeId).vars, ...user.vars };
    for (const key of Object.keys(SOFT_ACCENTS) as SoftAccentKey[]) {
        if (user.vars[key] === undefined) vars[key] = derivedSoft(user, key);
    }
    return { id: user.id, label: user.label, scheme: user.scheme, vars };
}

function isScheme(value: unknown): value is ThemeScheme {
    return value === 'dark' || value === 'light';
}

/** One stored user skin (from the channel doc, the boot cache or an import)
 *  as a UserTheme, or null when it has no `user.` id. A base that is not a
 *  built-in becomes paper for a light skin and night otherwise; a missing or
 *  unknown scheme is the base's; only string values of skin tokens are kept. */
export function normalizeUserTheme(raw: unknown): UserTheme | null {
    if (!raw || typeof raw !== 'object') return null;
    const stored = raw as Record<string, unknown>;
    if (typeof stored.id !== 'string' || !isUserThemeId(stored.id)) return null;
    const storedScheme = isScheme(stored.scheme) ? stored.scheme : undefined;
    const baseThemeId: BuiltinThemeId = typeof stored.baseThemeId === 'string' && isBuiltinThemeId(stored.baseThemeId)
        ? stored.baseThemeId
        : storedScheme === 'light' ? 'paper' : 'night';
    const vars: Partial<ThemeVars> = {};
    if (stored.vars && typeof stored.vars === 'object') {
        for (const [key, value] of Object.entries(stored.vars)) {
            if (key in THEME_CSS_VARS && typeof value === 'string') vars[key as keyof ThemeVars] = value;
        }
    }
    return {
        id: stored.id,
        label: typeof stored.label === 'string' ? stored.label : '',
        scheme: storedScheme ?? getTheme(baseThemeId).scheme,
        vars,
        baseThemeId,
        createdAt: typeof stored.createdAt === 'number' ? stored.createdAt : 0,
    };
}

/** A stored `userThemes` value as a library: each entry normalised, those
 *  without a `user.` id dropped, anything but an array an empty library. */
export function normalizeUserThemes(raw: unknown): UserTheme[] {
    if (!Array.isArray(raw)) return [];
    return raw.map(normalizeUserTheme).filter((u): u is UserTheme => u !== null);
}
