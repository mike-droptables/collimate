// What a renderer sees when it asks for the theme. Shared by NativeRenderer
// (api.getTheme in the shadow-DOM path) and the sandbox bridge (the
// SET_THEME message an iframe renderer receives), so both carry the same
// record: every skin token, keyed by its CSS name, read from <html>.
import { THEME_CSS_VARS, THEME_VAR_KEYS, type ThemeScheme } from './themes';

export interface ThemeFonts {
    ui: string;
    mono: string;
    prose: string;
}

export interface ThemeSnapshot {
    themeId: string;
    scheme: ThemeScheme;
    /** True once the host has delivered a real theme (the sandbox's
     *  placeholder before the first SET_THEME carries false). */
    known: true;
    /** Every skin token, keyed by CSS name (`--sheet-2`, `--font-prose`, …). */
    vars: Record<string, string>;
    fonts: ThemeFonts;
}

/** The live theme, read from <html>'s computed style and its data-theme /
 *  data-scheme attributes. */
export function readThemeSnapshot(): ThemeSnapshot {
    const root = document.documentElement;
    const cs = getComputedStyle(root);
    const vars: Record<string, string> = {};
    for (const key of THEME_VAR_KEYS) {
        const name = THEME_CSS_VARS[key];
        vars[name] = cs.getPropertyValue(name).trim();
    }
    return {
        themeId: root.getAttribute('data-theme') ?? 'unknown',
        scheme: root.getAttribute('data-scheme') === 'light' ? 'light' : 'dark',
        known: true,
        vars,
        fonts: {
            ui: vars[THEME_CSS_VARS.fontUi],
            mono: vars[THEME_CSS_VARS.fontMono],
            prose: vars[THEME_CSS_VARS.fontProse],
        },
    };
}
