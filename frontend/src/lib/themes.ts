// The skins. A skin is one record of tokens (ThemeVars) that ThemeContext
// writes on <html> under their CSS names (THEME_CSS_VARS), that Tailwind maps
// to utilities (app/globals.css) and that renderers receive verbatim in the
// theme snapshot. Beside the tokens each skin carries its scene (ThemeScene):
// the host-only record of the ground behind the stacks, its pattern, glows and
// vignette, and the selection edge width. Renderers never see it.
// User skins (./userThemes.ts) are a built-in plus overrides of both.
import { defaultFamily, stackFor } from './fonts';
import { themeImageUrl } from './api/appSettings';

export const BUILTIN_THEME_IDS = [
    'graphite', 'night', 'night-cool', 'night-clear', 'grid', 'cyberpunk', 'paper', 'precog',
] as const;
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

// ─── The scene ───────────────────────────────────────────────────────────────
// The host paints the ground behind the stacks itself (SceneBackground, one
// viewport-fixed layer under the scene), so the scene is a record of its own
// beside the 25 tokens: the tokens are the renderer contract, the scene is
// not. `--canvas` stays the chrome's ground (bars, journal, back cards); the
// scene's `base` is the ground the stacks stand on, the canvas unless a skin
// sets one. Two reach options let the scene run on behind chrome: the top
// bar (behindTopBar) and the overview's removed shelf between its cards
// (behindRemoved); that chrome fills from a host property that is the canvas
// or transparent (sceneChromeFill). A picture of the user's own (image, an
// upload in the CAS) covers the ground under the pattern and the lights.

export const SCENE_PATTERNS = ['none', 'grid', 'dots', 'fine-dots', 'scanlines'] as const;
export type ScenePattern = typeof SCENE_PATTERNS[number];

/** The pattern's minor pitch in CSS px; the major is five of them. Multiples
 *  of four, so the pitch lands on whole device pixels at the common scales
 *  (1.25×, 1.5×, 1.75×, 2×); SceneBackground snaps the rest. */
export const SCENE_PITCHES = [16, 20, 24] as const;
export type ScenePitch = typeof SCENE_PITCHES[number];
export const SCENE_MAJOR = 5;

/** The selected card's edge width in CSS px. */
export const SELECTION_EDGES = [1, 2] as const;
export type SelectionEdge = typeof SELECTION_EDGES[number];

/** A radial glow over the ground: a `#rrggbb` colour at an alpha in 0..1. */
export interface SceneGlow {
    color: string;
    alpha: number;
}

export interface ThemeScene {
    /** The ground behind the stacks, `#rrggbb`; null is the skin's canvas. */
    base: string | null;
    pattern: ScenePattern;
    /** The pattern's colour, `#rrggbb`. */
    ink: string;
    pitch: ScenePitch;
    /** The major marks' alpha, 0..1; the minor marks take half. */
    strength: number;
    /** Warm light from the top centre. */
    glowWarm: SceneGlow | null;
    /** Cool light from the bottom right. */
    glowCool: SceneGlow | null;
    /** The darkening toward the edges, 0..1. */
    vignette: number;
    edge: SelectionEdge;
    /** The scene continues up behind the app's top bar, whose own fill
     *  turns transparent (its bottom hairline stays). */
    behindTopBar: boolean;
    /** In the overview the scene shows through the removed shelf and its
     *  bar between the removed cards; the cards stay opaque. */
    behindRemoved: boolean;
    /** A picture over the ground, under the pattern and the lights: the CAS
     *  hash of an image uploaded to the server's app store (served at
     *  /api/app-settings/images/<hash>), scaled to cover and centred; null
     *  for none. */
    image: string | null;
}

export type SceneKey = keyof ThemeScene;

/** Whether `value` names a scene picture: a CAS hash, 64 hex digits. */
export function isSceneImage(value: unknown): value is string {
    return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
}

/** The scene's CSS custom properties on <html>, host-only. Colours arrive
 *  complete (alpha baked in), so the painting code holds no literal. */
export const SCENE_CSS_VARS = {
    ground: '--scene-ground',
    ink: '--scene-ink',
    inkFaint: '--scene-ink-faint',
    glowWarm: '--scene-glow-warm',
    glowCool: '--scene-glow-cool',
    vignette: '--scene-vignette',
    edge: '--selection-edge',
    /** The top bar's fill: the canvas, or transparent when the scene
     *  reaches behind it (ThemeScene.behindTopBar). */
    topBarGround: '--scene-top-bar-ground',
    /** The removed shelf's and bar's fill: the canvas, or transparent when
     *  the scene shows between the removed cards (ThemeScene.behindRemoved). */
    removedGround: '--scene-removed-ground',
    /** The picture as a CSS image: `url(...)` of its blob, or `none`. */
    image: '--scene-image',
} as const;

const SCENE_SHADE = '#000000';

/** `hex` (`#rrggbb`) at `alpha` as `#rrggbbaa`. */
function withAlpha(hex: string, alpha: number): string {
    const a = Math.round(Math.min(1, Math.max(0, alpha)) * 255);
    return `${hex}${a.toString(16).padStart(2, '0')}`;
}

/** The ground a skin's scene paints: its base, or the canvas. */
export function sceneGround(scene: ThemeScene, vars: ThemeVars): string {
    return scene.base ?? vars.canvas;
}

/** The scene's CSS custom properties, keyed by name. */
export function sceneCssVars(scene: ThemeScene, vars: ThemeVars): Record<string, string> {
    const glow = (g: SceneGlow | null) => (g ? withAlpha(g.color, g.alpha) : 'transparent');
    return {
        [SCENE_CSS_VARS.ground]: sceneGround(scene, vars),
        [SCENE_CSS_VARS.ink]: withAlpha(scene.ink, scene.strength),
        [SCENE_CSS_VARS.inkFaint]: withAlpha(scene.ink, scene.strength / 2),
        [SCENE_CSS_VARS.glowWarm]: glow(scene.glowWarm),
        [SCENE_CSS_VARS.glowCool]: glow(scene.glowCool),
        [SCENE_CSS_VARS.vignette]: withAlpha(SCENE_SHADE, scene.vignette),
        [SCENE_CSS_VARS.edge]: `${scene.edge}px`,
        [SCENE_CSS_VARS.topBarGround]: scene.behindTopBar ? 'transparent' : vars.canvas,
        [SCENE_CSS_VARS.removedGround]: scene.behindRemoved ? 'transparent' : vars.canvas,
        [SCENE_CSS_VARS.image]: scene.image ? `url("${themeImageUrl(scene.image)}")` : 'none',
    };
}

/** A chrome fill the scene may reach behind: `ground` (a SCENE_CSS_VARS
 *  name) as a var(), the canvas until ThemeContext writes it. */
export function sceneChromeFill(ground: typeof SCENE_CSS_VARS.topBarGround | typeof SCENE_CSS_VARS.removedGround): string {
    return `var(${ground}, var(${THEME_CSS_VARS.canvas}))`;
}

/** A flat ground on the canvas with a 1px selection edge; `ink` is what a
 *  pattern a user skin turns on is drawn in. */
function flatScene(ink: string): ThemeScene {
    return {
        base: null, pattern: 'none', ink, pitch: 16, strength: 0.12,
        glowWarm: null, glowCool: null, vignette: 0, edge: 1,
        behindTopBar: false, behindRemoved: false, image: null,
    };
}

export interface ThemeDef {
    id: ThemeId;
    label: string;
    /** Whether the skin paints on a dark or light ground; drives
     *  color-scheme, the `dark:` variant and the renderers' scheme. */
    scheme: ThemeScheme;
    vars: ThemeVars;
    scene: ThemeScene;
}

const shared = {
    fontUi: defaultFamily('ui').stack,
    fontMono: defaultFamily('mono').stack,
    fontProse: defaultFamily('prose').stack,
    radius: '8px',
} satisfies Partial<ThemeVars>;

/** The catalogue stack of `family`, which must be in lib/fonts. */
function catalogueStack(family: string): string {
    const stack = stackFor(family);
    if (!stack) throw new Error(`${family} is not in the font catalogue`);
    return stack;
}

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
    scene: flatScene('#a39d92'),
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
    scene: flatScene('#99a3ab'),
};

/** Night for clarity: a lighter ground and sheet, clearly brighter lines, a
 *  stronger brass and a 2px selection edge. */
const nightClear: ThemeDef = {
    id: 'night-clear',
    label: 'Night Clear',
    scheme: 'dark',
    vars: {
        ...night.vars,
        canvas: '#1d1a17',
        sheet: '#262320',
        sheet2: '#2e2a26',
        sheet3: '#36312c',
        line: '#605a52',
        line2: '#6e675d',
        ink: '#ece7de',
        ink2: '#b8b1a6',
        ink3: '#888175',
        brass: '#e3b066',
        brassSoft: 'rgba(227, 176, 102, 0.26)',
        teal: '#8cc0ba',
        tealSoft: 'rgba(140, 192, 186, 0.16)',
        tealSheet: '#212b28',
        red: '#df9888',
        redSoft: 'rgba(223, 152, 136, 0.18)',
        green: '#9dc8a0',
        codeBg: '#191714',
        proseInk: '#f0ebe2',
    },
    scene: { ...flatScene('#b8b1a6'), edge: 2 },
};

/** A neutral cool grey, no warm cast, on a faint dot field. */
const graphite: ThemeDef = {
    id: 'graphite',
    label: 'Graphite',
    scheme: 'dark',
    vars: {
        canvas: '#141618',
        sheet: '#1c1f22',
        sheet2: '#23272b',
        sheet3: '#2b3034',
        line: '#33383e',
        line2: '#41474e',
        ink: '#e1e4e8',
        ink2: '#a3a9b0',
        ink3: '#747b83',
        brass: '#c8a874',
        brassSoft: 'rgba(200, 168, 116, 0.15)',
        teal: '#7cb0b6',
        tealSoft: 'rgba(124, 176, 182, 0.14)',
        tealSheet: '#1a2326',
        red: '#d48b84',
        redSoft: 'rgba(212, 139, 132, 0.16)',
        green: '#8fbe97',
        codeBg: '#131517',
        proseInk: '#e6e8eb',
        shadow: '0 1px 0 rgba(0, 0, 0, 0.4), 0 8px 24px -16px rgba(0, 0, 0, 0.65)',
        lift: 'rgba(0, 0, 0, 0.78)',
        ...shared,
    },
    // Fine dots at 24/120, strong, and the scene runs on between the
    // overview's removed cards.
    scene: { ...flatScene('#a3a9b0'), pattern: 'fine-dots', pitch: 24, strength: 0.55, behindRemoved: true },
};

/** The earlier Collimate look: dark cards on a mid grey-blue ground ruled in
 *  a two-level grid, lit warm from the top and cool from the bottom right,
 *  darkening toward the edges. The chrome keeps a near-black canvas. */
const grid: ThemeDef = {
    id: 'grid',
    label: 'Grid',
    scheme: 'dark',
    vars: {
        canvas: '#08090c',
        sheet: '#1a1c23',
        sheet2: '#21242c',
        sheet3: '#292c35',
        line: '#353946',
        line2: '#464b5a',
        ink: '#eaeaea',
        ink2: '#a9acb4',
        ink3: '#7d808a',
        brass: '#c9a95c',
        brassSoft: 'rgba(201, 169, 92, 0.16)',
        teal: '#63b0a6',
        tealSoft: 'rgba(99, 176, 166, 0.14)',
        tealSheet: '#16242a',
        red: '#d0747d',
        redSoft: 'rgba(208, 116, 125, 0.16)',
        green: '#8cc08f',
        codeBg: '#121419',
        proseInk: '#ececec',
        shadow: '0 1px 0 rgba(0, 0, 0, 0.45), 0 10px 28px -16px rgba(0, 0, 0, 0.75)',
        lift: 'rgba(0, 0, 0, 0.75)',
        ...shared,
    },
    scene: {
        base: '#444b5a',
        pattern: 'grid',
        ink: '#b5b5b5',
        pitch: 16,
        strength: 0.15,
        glowWarm: { color: '#c3bcb5', alpha: 0.18 },
        glowCool: { color: '#38465f', alpha: 0.45 },
        vignette: 0.5,
        edge: 1,
        behindTopBar: false,
        behindRemoved: false,
        image: null,
    },
};

/** Neon noir: near-black, acid yellow selection, cyan live, hot red, fine
 *  scanlines, sharp corners and a mono interface face. */
const cyberpunk: ThemeDef = {
    id: 'cyberpunk',
    label: 'Neon Noir',
    scheme: 'dark',
    vars: {
        canvas: '#060708',
        sheet: '#0d0f12',
        sheet2: '#14181c',
        sheet3: '#1b2126',
        line: '#22313a',
        line2: '#33505a',
        ink: '#e9f3f4',
        ink2: '#9db1b6',
        ink3: '#6a7d83',
        brass: '#f3e600',
        brassSoft: 'rgba(243, 230, 0, 0.16)',
        teal: '#2de2e6',
        tealSoft: 'rgba(45, 226, 230, 0.12)',
        tealSheet: '#08191c',
        red: '#ff3b5c',
        redSoft: 'rgba(255, 59, 92, 0.18)',
        green: '#3fe08a',
        codeBg: '#050607',
        proseInk: '#e6eef0',
        shadow: '0 0 0 1px rgba(0, 0, 0, 0.6), 0 10px 24px -14px rgba(0, 0, 0, 0.85)',
        lift: 'rgba(0, 0, 0, 0.82)',
        ...shared,
        fontUi: catalogueStack('Geist Mono'),
        radius: '2px',
    },
    scene: {
        base: null,
        pattern: 'scanlines',
        ink: '#2de2e6',
        pitch: 16,
        strength: 0.1,
        glowWarm: { color: '#ff3b5c', alpha: 0.07 },
        glowCool: { color: '#2de2e6', alpha: 0.09 },
        vignette: 0.45,
        edge: 1,
        behindTopBar: false,
        behindRemoved: false,
        image: null,
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
    scene: flatScene('#5d646b'),
};

/** Glass at daylight: pale blue-grey sheets, cyan live, hairlines, a dot
 *  field, round corners and Manrope. */
const precog: ThemeDef = {
    id: 'precog',
    label: 'Daylight Glass',
    scheme: 'light',
    vars: {
        canvas: '#dde5ec',
        sheet: '#f5f9fc',
        sheet2: '#ebf1f6',
        sheet3: '#e1e9f0',
        line: '#d3dde6',
        line2: '#b7c6d3',
        ink: '#13202c',
        ink2: '#4a5b6a',
        ink3: '#7b8b9a',
        brass: '#a85d14',
        brassSoft: 'rgba(168, 93, 20, 0.12)',
        teal: '#0a7fa3',
        tealSoft: 'rgba(10, 127, 163, 0.10)',
        tealSheet: '#e4f2f8',
        red: '#b63c3c',
        redSoft: 'rgba(182, 60, 60, 0.10)',
        green: '#2c7a52',
        codeBg: '#ebf1f5',
        proseInk: '#111c26',
        shadow: '0 1px 1px rgba(20, 40, 60, 0.06), 0 12px 28px -18px rgba(20, 40, 60, 0.28)',
        lift: 'rgba(20, 36, 52, 0.22)',
        ...shared,
        fontUi: catalogueStack('Manrope'),
        radius: '12px',
    },
    scene: {
        base: null,
        pattern: 'dots',
        ink: '#4f7590',
        pitch: 24,
        strength: 0.3,
        glowWarm: { color: '#ffffff', alpha: 0.55 },
        glowCool: { color: '#8fd3ea', alpha: 0.3 },
        vignette: 0,
        edge: 1,
        behindTopBar: false,
        behindRemoved: false,
        image: null,
    },
};

export const THEMES: ThemeDef[] = [graphite, night, nightCool, nightClear, grid, cyberpunk, paper, precog];

/** The server's default theme until one is chosen, and the theme an id
 *  that resolves to nothing falls back to. */
export const DEFAULT_THEME_ID: BuiltinThemeId = 'graphite';

export function getTheme(id: ThemeId): ThemeDef {
    return THEMES.find(t => t.id === id) ?? graphite;
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
