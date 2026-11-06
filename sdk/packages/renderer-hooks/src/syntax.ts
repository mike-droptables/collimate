// SPDX-License-Identifier: MIT
//
// syntaxPalette — code-highlighting colours derived from the skin.
//
// A skin names four hues (teal, brass, green, red) and three inks; a syntax
// highlighter needs more roles than that. Every role here is one of those
// tokens, or one of them moved a single OKLCH lightness step, so Monaco,
// CodeMirror and any other highlighter draw code in the same colours:
//
//   comment      ink-3            keyword    teal         string   green
//   punctuation  ink-2            tag        teal         regexp   green, stepped
//   operator     ink-2            type       teal, stepped
//   variable     ink              number     brass        added    green
//   invalid      red              attribute  brass        removed  red
//                                 function   brass, stepped
//                                 modified   brass
//
// Every role's hue is nudged away from the ground in NUDGE steps of OKLCH
// lightness until its WCAG contrast ratio is at least MIN_CONTRAST against
// each code ground the skin names (--sheet, the editor's ground, and
// --code-bg, code blocks'): lighter on a dark ground, darker on a light one,
// the direction read from --sheet's luminance, not from the record's scheme.
// A hue that reaches black or white short of the floor is nudged the other
// way instead, and failing both takes --ink. "Stepped" roles then sit one
// LIGHTNESS_STEP further in the same direction, so a stepped role reads as a
// sibling of its hue and stays apart from it.
//
// Syntax colour is opaque: a translucent source token is composited over
// --sheet, as it would paint there. A user skin may write a token in a form
// this helper cannot read (hsl(), oklch(), a named colour); such a role takes
// the next token in its SOURCES chain, ending at --ink. The palette is null
// only when --sheet or --ink does not resolve (the placeholder before the
// host's first theme, or a skin written in such forms).

import { resolveVar, toHex8 } from './color';
import type { RendererTheme, ThemeVarName } from './types';

export type SyntaxRole =
    | 'comment' | 'keyword' | 'string' | 'number' | 'type' | 'function'
    | 'variable' | 'operator' | 'punctuation' | 'tag' | 'attribute' | 'regexp'
    | 'invalid' | 'added' | 'removed' | 'modified';

/** Every role as a lowercase `#rrggbb`. */
export type SyntaxPalette = Readonly<Record<SyntaxRole, string>>;

export const SYNTAX_ROLES: readonly SyntaxRole[] = Object.freeze([
    'comment', 'keyword', 'string', 'number', 'type', 'function',
    'variable', 'operator', 'punctuation', 'tag', 'attribute', 'regexp',
    'invalid', 'added', 'removed', 'modified',
] as const);

/** Each role's source tokens, first readable one wins, and whether it takes
 *  the lightness step. Every chain ends at --ink, which must resolve. */
const SOURCES: Readonly<Record<SyntaxRole, readonly [readonly ThemeVarName[], boolean]>> = {
    comment: [['--ink-3', '--ink-2', '--ink'], false],
    keyword: [['--teal', '--ink'], false],
    string: [['--green', '--ink'], false],
    number: [['--brass', '--ink'], false],
    type: [['--teal', '--ink'], true],
    function: [['--brass', '--ink'], true],
    variable: [['--ink'], false],
    operator: [['--ink-2', '--ink'], false],
    punctuation: [['--ink-2', '--ink'], false],
    tag: [['--teal', '--ink'], false],
    attribute: [['--brass', '--ink'], false],
    regexp: [['--green', '--ink'], true],
    invalid: [['--red', '--ink'], false],
    added: [['--green', '--ink'], false],
    removed: [['--red', '--ink'], false],
    modified: [['--brass', '--ink'], false],
};

/** The OKLCH lightness step that separates a stepped role from its hue. */
const LIGHTNESS_STEP = 0.08;
/** The OKLCH lightness increment of the contrast nudge. */
const NUDGE = 0.01;
/** WCAG contrast floor for every role against every code ground. */
const MIN_CONTRAST = 3;
/** A ground at or below this relative luminance is dark: roles step lighter. */
const DARK_GROUND = 0.18;

type Rgb = readonly [number, number, number];   // sRGB channels, 0..1
type Lab = readonly [number, number, number];   // OKLab L, a, b

/** `#rrggbb` or `#rrggbbaa` as sRGB channels; alpha is read by `parseAlpha`. */
function parseRgb(hex8: string): Rgb {
    return [1, 3, 5].map((i) => parseInt(hex8.slice(i, i + 2), 16) / 255) as unknown as Rgb;
}

/** The alpha of a `#rrggbbaa`, 0..1 (1 for `#rrggbb`). */
function parseAlpha(hex8: string): number {
    return hex8.length === 9 ? parseInt(hex8.slice(7, 9), 16) / 255 : 1;
}

function formatHex(rgb: Rgb): string {
    return '#' + rgb
        .map((c) => Math.round(Math.max(0, Math.min(1, c)) * 255).toString(16).padStart(2, '0'))
        .join('');
}

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const fromLinear = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055);

function toOklab(rgb: Rgb): Lab {
    const [r, g, b] = rgb.map(toLinear);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    ];
}

/** OKLab to sRGB, unclamped: a channel outside 0..1 is out of gamut. */
function fromOklab([L, a, b]: Lab): Rgb {
    const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
    const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
    const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
    return [
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    ].map(fromLinear) as unknown as Rgb;
}

const EPS = 1e-4;
const inGamut = (rgb: Rgb) => rgb.every((c) => c >= -EPS && c <= 1 + EPS);

/** `rgb` with its OKLCH lightness set to `L` and its hue kept; chroma is
 *  reduced only as far as the colour needs to stay inside sRGB. */
function withLightness(rgb: Rgb, L: number): Rgb {
    const [, a, b] = toOklab(rgb);
    const target = Math.max(0, Math.min(1, L));
    let k = 1;
    let out = fromOklab([target, a, b]);
    while (!inGamut(out) && k > 0.01) {
        k *= 0.95;
        out = fromOklab([target, a * k, b * k]);
    }
    return out.map((c) => Math.max(0, Math.min(1, c))) as unknown as Rgb;
}

function luminance(rgb: Rgb): number {
    const [r, g, b] = rgb.map((c) => toLinear(Math.max(0, Math.min(1, c))));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(x: Rgb, y: Rgb): number {
    const [hi, lo] = [luminance(x), luminance(y)].sort((p, q) => q - p);
    return (hi + 0.05) / (lo + 0.05);
}

/** A token's colour as opaque sRGB, or null when it does not resolve to a
 *  hex or rgb() colour. A translucent token is composited over `ground`
 *  (a*src + (1-a)*ground); with no ground its alpha is dropped. */
function tokenRgb(theme: RendererTheme, name: ThemeVarName, ground: Rgb | null): Rgb | null {
    const hex = toHex8(resolveVar(theme, name));
    if (!hex) return null;
    const rgb = parseRgb(hex);
    const a = parseAlpha(hex);
    if (!ground || a >= 1) return rgb;
    return rgb.map((c, i) => a * c + (1 - a) * ground[i]) as unknown as Rgb;
}

/** `source` at OKLCH lightness `L`, moved by `dir` in NUDGE steps until it
 *  clears MIN_CONTRAST on every ground; null when it reaches black or white
 *  first. The colour that ships is the 8-bit one, so contrast is measured on
 *  it, not before rounding. */
function nudge(source: Rgb, L: number, exact: boolean, dir: number, grounds: readonly Rgb[]): string | null {
    let hex = formatHex(exact ? source : withLightness(source, L));
    const fails = () => grounds.some((g) => contrast(parseRgb(hex), g) < MIN_CONTRAST);
    while (fails()) {
        if (L <= 0 || L >= 1) return null;
        L = Math.max(0, Math.min(1, L + dir * NUDGE));
        hex = formatHex(withLightness(source, L));
    }
    return hex;
}

/**
 * The skin's syntax palette, every role a lowercase `#rrggbb`, frozen; null
 * when --sheet or --ink does not resolve (the placeholder before the host's
 * first theme), which leaves the library's own colours in place.
 */
export function syntaxPalette(theme: RendererTheme): SyntaxPalette | null {
    const sheet = tokenRgb(theme, '--sheet', null);
    if (!sheet) return null;
    const ink = tokenRgb(theme, '--ink', sheet);
    if (!ink) return null;
    const codeBg = tokenRgb(theme, '--code-bg', sheet);
    const grounds = codeBg ? [sheet, codeBg] : [sheet];
    // Away from the ground: lighter on a dark ground, darker on a light one.
    const away = luminance(sheet) <= DARK_GROUND ? 1 : -1;
    const inkHex = formatHex(ink);
    const palette = {} as Record<SyntaxRole, string>;
    for (const role of SYNTAX_ROLES) {
        const [chain, stepped] = SOURCES[role];
        let source = ink;
        for (const name of chain) {
            const rgb = tokenRgb(theme, name, sheet);
            if (rgb) { source = rgb; break; }
        }
        // The role's hue at 3:1, nudged away from the ground or, short of
        // the floor at black or white, toward it; failing both, --ink.
        let dir = away;
        let hex = nudge(source, toOklab(source)[0], true, dir, grounds);
        if (hex === null) {
            dir = -away;
            hex = nudge(source, toOklab(source)[0], true, dir, grounds);
        }
        if (hex === null) {
            palette[role] = inkHex;
            continue;
        }
        // A stepped role sits one step past its hue's floored colour, so the
        // two stay apart even where the floor moved the hue.
        if (stepped) {
            const L = toOklab(parseRgb(hex))[0] + dir * LIGHTNESS_STEP;
            hex = nudge(source, L, false, dir, grounds) ?? hex;
        }
        palette[role] = hex;
    }
    return Object.freeze(palette);
}
