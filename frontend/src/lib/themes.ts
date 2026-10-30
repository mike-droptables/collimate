// The skins. A skin is one record of tokens (ThemeVars) that ThemeContext
// writes on <html> under their CSS names (THEME_CSS_VARS), that Tailwind maps
// to utilities (app/globals.css) and that renderers receive verbatim in the
// theme snapshot. The three built-ins carry the Night Workbench values;
// user skins (./userThemes.ts) are a built-in plus overrides.
import { defaultFamily } from './fonts';

export const BUILTIN_THEME_IDS = ['night', 'night-cool', 'paper'] as const;
export type BuiltinThemeId = typeof BUILTIN_THEME_IDS[number];
export type ThemeId = BuiltinThemeId | `user.${string}`;
export type ThemeScheme = 'dark' | 'light';

export function isBuiltinThemeId(id: string): id is BuiltinThemeId {
    return (BUILTIN_THEME_IDS as readonly string[]).includes(id);
}

export function isUserThemeId(id: string): id is `user.${string}` {
    return typeof id === 'string' && id.startsWith('user.');
}

export interface ThemeVars {
    /** The ground behind every stack and bar. */
    canvas: string;
    /** Panels and cells. */
    sheet: string;
    /** Raised rows and hover fills. */
    sheet2: string;
    /** The highest surface: pressed fills, a primary button's hover. */
    sheet3: string;
    /** Hairlines between regions. */
    line: string;
    /** Control borders. */
    line2: string;
    /** Primary text. */
    ink: string;
    /** Secondary text. */
    ink2: string;
    /** Hints, placeholders, disabled text. */
    ink3: string;
    /** The selection accent, and only that. */
    brass: string;
    brassSoft: string;
    /** Live and on-states. */
    teal: string;
    tealSoft: string;
    /** The head fill of a special (generator or renderer) stack. */
    tealSheet: string;
    /** Destructive. */
    red: string;
    redSoft: string;
    /** Pass. */
    green: string;
    /** The ground of code blocks and the terminal. */
    codeBg: string;
    /** Body text of prose (markdown, documents). */
    proseInk: string;
    /** The drop shadow of menus and lifted surfaces. */
    shadow: string;
    /** Scrims and backdrops behind lifted surfaces. */
    lift: string;
    /** CSS font-family stacks, one per role (see lib/fonts). */
    fontUi: string;
    fontMono: string;
    fontProse: string;
    /** The large corner radius; the smaller radii step down from it. */
    radius: string;
}

export type ThemeVarKey = keyof ThemeVars;

/** Every token's CSS custom-property name. The one table every writer
 *  (ThemeContext, the sandbox) and reader (the theme snapshot) uses. */
export const THEME_CSS_VARS = {
    canvas: '--canvas',
    sheet: '--sheet',
    sheet2: '--sheet-2',
    sheet3: '--sheet-3',
    line: '--line',
    line2: '--line-2',
    ink: '--ink',
    ink2: '--ink-2',
    ink3: '--ink-3',
    brass: '--brass',
    brassSoft: '--brass-soft',
    teal: '--teal',
    tealSoft: '--teal-soft',
    tealSheet: '--teal-sheet',
    red: '--red',
    redSoft: '--red-soft',
    green: '--green',
    codeBg: '--code-bg',
    proseInk: '--prose-ink',
    shadow: '--shadow',
    lift: '--lift',
    fontUi: '--font-ui',
    fontMono: '--font-mono',
    fontProse: '--font-prose',
    radius: '--radius',
} as const satisfies Record<ThemeVarKey, `--${string}`>;

export const THEME_VAR_KEYS = Object.keys(THEME_CSS_VARS) as ThemeVarKey[];

/** A skin's tokens keyed by CSS name — the shape renderers receive. */
export function themeCssVars(vars: ThemeVars): Record<string, string> {
    const out: Record<string, string> = {};
    for (const key of THEME_VAR_KEYS) out[THEME_CSS_VARS[key]] = vars[key];
    return out;
}

export interface ThemeDef {
    id: ThemeId;
    label: string;
    /** Whether the skin paints on a dark or light ground; drives
     *  color-scheme, the `dark:` variant and the renderers' scheme. */
    scheme: ThemeScheme;
    vars: ThemeVars;
}

const shared = {
    fontUi: defaultFamily('ui').stack,
    fontMono: defaultFamily('mono').stack,
    fontProse: defaultFamily('prose').stack,
    radius: '8px',
} satisfies Partial<ThemeVars>;

const night: ThemeDef = {
    id: 'night',
    label: 'Night',
    scheme: 'dark',
    vars: {
        canvas: '#151311',
        sheet: '#1c1a17',
        sheet2: '#23201c',
        sheet3: '#2a2621',
        line: '#494641',
        line2: '#3f3931',
        ink: '#e2ddd4',
        ink2: '#a39d92',
        ink3: '#6f695f',
        brass: '#c69d64',
        brassSoft: 'rgba(198, 157, 100, 0.16)',
        teal: '#7fb0ab',
        tealSoft: 'rgba(127, 176, 171, 0.14)',
        tealSheet: '#1a211f',
        red: '#cf8b7c',
        redSoft: 'rgba(207, 139, 124, 0.16)',
        green: '#90b893',
        codeBg: '#121008',
        proseInk: '#e9e4da',
        shadow: '0 1px 0 rgba(0, 0, 0, 0.4), 0 8px 24px -16px rgba(0, 0, 0, 0.65)',
        lift: 'rgba(0, 0, 0, 0.8)',
        ...shared,
    },
};

const nightCool: ThemeDef = {
    id: 'night-cool',
    label: 'Night · cool',
    scheme: 'dark',
    vars: {
        canvas: '#10161b',
        sheet: '#171d23',
        sheet2: '#1d252c',
        sheet3: '#232c34',
        line: '#2a333b',
        line2: '#344049',
        ink: '#dbdfe3',
        ink2: '#99a3ab',
        ink3: '#626e77',
        brass: '#b9966b',
        brassSoft: 'rgba(185, 150, 107, 0.14)',
        teal: '#76a9a6',
        tealSoft: 'rgba(118, 169, 166, 0.14)',
        tealSheet: '#172226',
        red: '#c9857a',
        redSoft: 'rgba(201, 133, 122, 0.16)',
        green: '#88b592',
        codeBg: '#131920',
        proseInk: '#e4e7ea',
        shadow: '0 1px 0 rgba(0, 0, 0, 0.35), 0 8px 24px -16px rgba(0, 0, 0, 0.6)',
        lift: 'rgba(0, 0, 0, 0.75)',
        ...shared,
    },
};

const paper: ThemeDef = {
    id: 'paper',
    label: 'Paper',
    scheme: 'light',
    vars: {
        canvas: '#e6e4de',
        sheet: '#f8f7f3',
        sheet2: '#f1efea',
        sheet3: '#e9e7e1',
        line: '#d4d0c7',
        line2: '#c4bfb5',
        ink: '#1e2227',
        ink2: '#5d646b',
        ink3: '#9a9fa4',
        brass: '#8a6d3b',
        brassSoft: 'rgba(138, 109, 59, 0.12)',
        teal: '#2e7773',
        tealSoft: 'rgba(46, 119, 115, 0.10)',
        tealSheet: '#f1f5f3',
        red: '#a3463a',
        redSoft: 'rgba(163, 70, 58, 0.10)',
        green: '#3d7750',
        codeBg: '#efede7',
        proseInk: '#1a1e22',
        shadow: '0 1px 1px rgba(40, 36, 28, 0.06), 0 10px 24px -18px rgba(40, 36, 28, 0.25)',
        lift: 'rgba(40, 36, 28, 0.22)',
        ...shared,
    },
};

export const THEMES: ThemeDef[] = [night, nightCool, paper];

export const DEFAULT_THEME_ID: BuiltinThemeId = 'night';

export function getTheme(id: ThemeId): ThemeDef {
    return THEMES.find(t => t.id === id) ?? night;
}

/** `color` as an rgba() at the alpha of `like` (an rgba() soft token), so a
 *  changed accent keeps its skin's soft-fill strength. A non-hex `color`
 *  or an opaque `like` returns `like` unchanged. */
export function softVariant(color: string, like: string): string {
    const hex = /^#([0-9a-f]{6})$/i.exec(color.trim());
    const alpha = /rgba?\([^)]*,\s*([\d.]+)\s*\)/.exec(like)?.[1];
    if (!hex || alpha === undefined) return like;
    const n = parseInt(hex[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
