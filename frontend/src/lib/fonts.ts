// The bundled font catalogue: open-licensed faces per role. The theme editor
// offers each role's families in a select, and a skin stores the chosen
// family as a CSS font-family stack (ThemeVars.fontUi / fontMono /
// fontProse). DEFAULT_FAMILIES names the family each role's built-in skins
// use; a user skin inherits it from its base until it picks another.
//
// Every family ships as self-hosted woff2 under public/fonts/<slug>/ with its
// licence text, declared in public/fonts/fonts.css. Two sources feed it:
// Google Fonts (the css2 API's latin and latin-ext files, with OFL.txt) and
// the core collection of Nico Verbruggen's ebook-fonts at a pinned commit
// (four static TTFs per family, subset to latin and latin-ext, with
// LICENSE.txt). scripts/build_fonts.mjs reads this catalogue and regenerates
// the files, fonts.css and the fonts section of THIRD_PARTY_LICENSES; run it
// after changing the list.

export type FontRole = 'ui' | 'mono' | 'prose';

/** The licence a family is redistributed under, as an SPDX expression.
 *  Domitian (NV Palatium) is offered under any of three licences; its GNU
 *  AGPL option carries URW's PostScript/PDF embedding exception. */
export type FontLicense =
    | 'OFL-1.1'
    | 'Bitstream-Charter'
    | 'OFL-1.1 OR LPPL-1.3c OR AGPL-3.0-only WITH LicenseRef-URW-font-exception';

/** The four static styles of an ebook-fonts family, by file name. */
export interface FontFiles {
    regular: string;
    italic: string;
    bold: string;
    boldItalic: string;
}

/** The family a modified face derives from, and where its licence text lives. */
export interface FontOrigin {
    /** The upstream family and its designers. */
    family: string;
    /** The raw URLs of the upstream licence texts, at pinned commits. */
    licenseText: string[];
    /** How the licence reads against the font's own notices, where they
     *  differ; written into LICENSE.txt and THIRD_PARTY_LICENSES. */
    note?: string;
}

export type FontSource =
    | { provider: 'google'; family: string }
    | {
        provider: 'github';
        repo: 'nicoverbruggen/ebook-fonts';
        /** The commit the files are taken from. */
        ref: string;
        dir: 'fonts/core';
        files: FontFiles;
        origin: FontOrigin;
    };

export interface FontFamily {
    role: FontRole;
    /** The CSS family name, as `@font-face` declares it. */
    family: string;
    /** What the editor's select shows. */
    label: string;
    /** The full font-family stack a skin stores. */
    stack: string;
    weights: number[];
    italic: boolean;
    /** The directory under public/fonts/ that holds the family's files. */
    slug: string;
    license: FontLicense;
    /** Where the bundled files come from. */
    source: FontSource;
}

/** The generic fallbacks behind each role's family. */
const FALLBACKS: Record<FontRole, string> = {
    ui: 'system-ui, -apple-system, "Segoe UI", sans-serif',
    mono: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
    prose: 'Georgia, "Times New Roman", serif',
};

const WEIGHTS: Record<FontRole, number[]> = {
    ui: [400, 500, 600],
    mono: [400, 500],
    prose: [400, 500, 600],
};

/** The ebook-fonts commit the core collection is bundled from. */
export const EBOOK_FONTS_REF = 'f875aca16d3e871a01af52c854b67bc7f7abce9d';

/** The google/fonts commit the original families' OFL.txt files are read at. */
const GOOGLE_FONTS_REF = '5e8a3ba899557829a76cfdac30fa512bda91d7ca';

function googleOfl(dir: string): string {
    return `https://raw.githubusercontent.com/google/fonts/${GOOGLE_FONTS_REF}/ofl/${dir}/OFL.txt`;
}

/** The directory name for `family`: lowercase, words joined by hyphens. */
export function fontSlug(family: string): string {
    return family.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

function face(role: FontRole, family: string, weights = WEIGHTS[role]): FontFamily {
    return {
        role,
        family,
        label: family,
        stack: `"${family}", ${FALLBACKS[role]}`,
        weights,
        italic: role === 'prose',
        slug: fontSlug(family),
        license: 'OFL-1.1',
        source: { provider: 'google', family },
    };
}

/** An ebook-fonts core family: `family` is the name its TTFs' name table
 *  declares, `stem` the prefix of its file names. Each ships Regular, Italic,
 *  Bold and Bold Italic, so 400 and 700 with italics. */
function ebookFace(role: FontRole, family: string, stem: string, license: FontLicense, origin: FontOrigin): FontFamily {
    return {
        role,
        family,
        label: family.replace(/^NV /, ''),
        stack: `"${family}", ${FALLBACKS[role]}`,
        weights: [400, 700],
        italic: true,
        slug: fontSlug(family),
        license,
        source: {
            provider: 'github',
            repo: 'nicoverbruggen/ebook-fonts',
            ref: EBOOK_FONTS_REF,
            dir: 'fonts/core',
            files: {
                regular: `${stem}-Regular.ttf`,
                italic: `${stem}-Italic.ttf`,
                bold: `${stem}-Bold.ttf`,
                boldItalic: `${stem}-BoldItalic.ttf`,
            },
            origin,
        },
    };
}

const CARTISSE_LICENSE = 'https://raw.githubusercontent.com/nicoverbruggen/cartisse/bf7254a533bb29d6d24884435bc891524ff40d05/LICENSE';
const DOMITIAN = 'https://raw.githubusercontent.com/dbenjaminmiller/domitian/e3da170c8bf0de5871481303658c54eadd233518';

export const FONT_CATALOGUE: Record<FontRole, readonly FontFamily[]> = {
    ui: [
        ...['Hanken Grotesk', 'Inter', 'IBM Plex Sans', 'Source Sans 3', 'Manrope'].map(f => face('ui', f)),
        ebookFace('ui', 'NV Jost', 'NV_Jost', 'OFL-1.1',
            { family: 'Jost by Owen Earl (indestructible type*)', licenseText: [googleOfl('jost')] }),
        ebookFace('ui', 'NV Legible Next', 'NV_Legible_Next', 'OFL-1.1',
            {
                family: 'Atkinson Hyperlegible Next by the Braille Institute',
                licenseText: [googleOfl('atkinsonhyperlegiblenext')],
                note: 'The font\'s name ID 13 carries the Braille Institute\'s original no-derivatives notice from its '
                    + 'standalone release. The copyright holder publishes Atkinson Hyperlegible Next under the SIL Open '
                    + 'Font License 1.1 (https://github.com/googlefonts/atkinson-hyperlegible-next, OFL.txt), and these '
                    + 'files are redistributed under that licence.',
            }),
    ],
    prose: [
        ...['Alegreya', 'Source Serif 4', 'Literata', 'Lora', 'Crimson Pro'].map(f => face('prose', f)),
        ebookFace('prose', 'Libron', 'Libron', 'OFL-1.1',
            { family: 'Newsreader by Production Type, by way of Readerly', licenseText: [googleOfl('newsreader')] }),
        ebookFace('prose', 'Sourcerer', 'Sourcerer', 'OFL-1.1',
            { family: 'Source Serif 4 by Adobe', licenseText: [googleOfl('sourceserif4')] }),
        ebookFace('prose', 'Cartisse', 'Cartisse', 'Bitstream-Charter',
            { family: 'XCharter by Michael Sharpe, an extension of Bitstream Charter by Matthew Carter', licenseText: [CARTISSE_LICENSE] }),
        // The core's NV Charis is a modified Charis, and Charis's OFL reserves
        // the names "Charis" and "SIL", so the unmodified original stands in.
        face('prose', 'Charis SIL', [400, 700]),
        ebookFace('prose', 'NV Garamond', 'NV_Garamond', 'OFL-1.1',
            { family: 'EB Garamond by Georg Duffner and Octavio Pardo', licenseText: [googleOfl('ebgaramond')] }),
        ebookFace('prose', 'NV Bitter', 'NV_Bitter', 'OFL-1.1',
            { family: 'Bitter by Sol Matas', licenseText: [googleOfl('bitter')] }),
        ebookFace('prose', 'NV Palatium', 'NV_Palatium',
            'OFL-1.1 OR LPPL-1.3c OR AGPL-3.0-only WITH LicenseRef-URW-font-exception',
            { family: 'Domitian by Daniel Benjamin Miller, an extension of URW Palladio after Hermann Zapf\'s Palatino', licenseText: [`${DOMITIAN}/README.md`, `${DOMITIAN}/COPYING`] }),
    ],
    mono: ['IBM Plex Mono', 'JetBrains Mono', 'Source Code Pro', 'Fira Code', 'Geist Mono'].map(f => face('mono', f)),
};

/** The family every built-in skin uses per role: the one place the default
 *  faces are named. The skins read it, so a user skin's inherited (reset)
 *  value and the editor's readout follow; themes.test pins globals.css's
 *  pre-theme paint to night's stacks. */
export const DEFAULT_FAMILIES: Readonly<Record<FontRole, string>> = {
    ui: 'Hanken Grotesk',
    prose: 'Lora',
    mono: 'JetBrains Mono',
};

/** The family the built-in skins use for `role`. */
export function defaultFamily(role: FontRole): FontFamily {
    const found = FONT_CATALOGUE[role].find(f => f.family === DEFAULT_FAMILIES[role]);
    if (!found) throw new Error(`default ${role} family ${DEFAULT_FAMILIES[role]} is not in the catalogue`);
    return found;
}

/** The catalogue entry named `family`, in any role. */
export function fontFamily(family: string): FontFamily | undefined {
    for (const role of Object.keys(FONT_CATALOGUE) as FontRole[]) {
        const found = FONT_CATALOGUE[role].find(f => f.family === family);
        if (found) return found;
    }
    return undefined;
}

/** The stack a skin stores for the catalogue family `family`. */
export function stackFor(family: string): string | undefined {
    return fontFamily(family)?.stack;
}

/** The catalogue entry a stored stack names (by its first family). */
export function familyForStack(stack: string): FontFamily | undefined {
    const first = stack.split(',')[0]?.trim().replace(/^["']|["']$/g, '');
    return first ? fontFamily(first) : undefined;
}
