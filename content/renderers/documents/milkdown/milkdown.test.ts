// SPDX-License-Identifier: MIT
//
// milkdown.test.ts — the markdown editor's skin CSS (R02), tested off-DOM.
//
// The renderer injects Crepe's own sheets and then `milkdownCss()`: the
// --crepe-* variables mapped onto the skin tokens, the mock's prose (the .md
// block of docs/plan1006/input/night-workbench-mock.html), Crepe's floating
// chrome in the app's menu vocabulary, and the editor's own behaviour rules.
// These cases pin that every variable Crepe's sheets read is mapped, that no
// colour or font literal survives, and that the prose carries the mock's values.
// Fenced code (R02 W2b) is highlighted in the SDK's syntaxPalette: the cases
// pin the Lezer tag-to-role map, that the highlight names no colour of its
// own (each tag reads a --colmd-syntax-<role> property the palette fills),
// and that it reaches Crepe's own CodeMirror with One Dark left out, every
// colour rule of CodeMirror's light base theme restated on the tokens
// (code-highlight.ts).
// Insert and no uploads (§3b): the href and image/link helpers, the file
// list's candidates and its place at the caret, the pasted-HTML image filter
// (against a stand-in template, since node has no DOM), a drop of the cell's
// files (against a stand-in view), and source pins that every Crepe upload
// path is off and the insertion writes plain markdown.
// Embeds (§3c): the attribute-block grammar, byte-for-byte round trips of
// every embed form through the real pinned Milkdown parser and serializer,
// the remark transform's independence from Crepe's remark-image-block, the
// previews by kind, the layout, and the toolbar's target and writes against
// real ProseMirror states.
// The style (RS-03): the library themes' sheets and their lift onto :root,
// the prose and chrome on the --crepe-* variables, the font and the text
// size from the host's style record (never localStorage), and the caret at
// the end on a press below the last block.
//
// The harness bundles main.tsx with its CDN/SDK imports stubbed
// (scripts/bundle_renderer_test.mjs) and passes MILKDOWN_RENDERER_BUNDLE; see
// tests/test_milkdown_renderer_model.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// The SDK's palette, from source: the bundle carries the same implementation.
import { SYNTAX_ROLES, syntaxPalette } from '../../../../sdk/packages/renderer-hooks/src/syntax.ts';

const BUNDLE = process.env.MILKDOWN_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'MILKDOWN_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_milkdown_renderer_model.py does this).',
    );
}

type Box = { left: number; right: number; top: number; bottom: number };
type TagRole = { tag: string; role: string; fontStyle?: string; fontWeight?: string; textDecoration?: string };
let m: {
    CREPE_TOKEN_CSS: string;
    CODE_BLOCK_CSS: string;
    CODE_BLOCK_CREPE_CSS: string;
    milkdownCss: () => string;
    LEZER_TAG_ROLES: readonly TagRole[];
    syntaxVar: (role: string) => string;
    lezerTag: (tagsNs: any, expr: string) => any;
    highlightSpecs: (tagsNs: any) => Array<Record<string, any>>;
    syntaxVars: (palette: Readonly<Record<string, string>> | null) => Record<string, string | null>;
    resolveCellPath: (mdPath: string, href: string) => string | null;
    followLinkClick: (e: any, mdPath: string, readOnly: boolean, reveal: (cellPath: string, options: { newTab: boolean }) => void) => boolean;
    relativeHref: (mdPath: string, cellPath: string) => string;
    insertionFor: (mdPath: string, cellPath: string) => { kind: 'image' | 'link'; href: string; label: string };
    insertCandidates: (paths: string[], mdPath: string) => string[];
    INSERT_LIST_WIDTH: number;
    INSERT_LIST_MARGIN: number;
    INSERT_LIST_GAP: number;
    INSERT_LIST_MIN_HEIGHT: number;
    placeInsertList: (anchor: Box, column: Box, width: number) => { left: number; top: number; width: number; maxHeight: number; above: boolean };
    allowCellFileDrag: (view: any, event: any) => boolean;
    dropCellFiles: (view: any, event: any, api: any, place: (pos: number, paths: string[]) => void) => boolean;
    belowLastBlock: (pm: any, clientY: number) => boolean;
    TEXT_SIZE: Readonly<{ min: number; max: number; step: number; default: number }>;
    textSizeZoom: (textSize: unknown) => number;
    MILKDOWN_THEMES: ReadonlyArray<{ id: string; label: string }>;
    LIBRARY_THEME_STYLE_ID: string;
    libraryThemeUrl: (themeId: unknown, scheme: string) => string | null;
    liftLibraryTheme: (css: string) => string;
    fontStack: (family: string | null, families: ReadonlyArray<{ family: string; stack: string }>) => string | null;
    stripPastedImages: (html: string) => string;
    documentSnapshotView: (note: () => void, external: () => boolean) => { update: (view: any, prevState: any) => void };
};
before(async () => {
    m = await import(pathToFileURL(BUNDLE).href);
});

const SOURCE = fs.readFileSync(
    path.join(process.cwd(), 'content', 'renderers', 'documents', 'milkdown', 'main.tsx'), 'utf8');

// Every --crepe-* variable read by the sheets crepeThemeCss() loads
// (@milkdown/crepe@7.22.3 lib/theme/common, without the diff-*-bg/-text
// variables diff.css derives itself from the two diff colours, and
// --crepe-base-font-size, a size every sheet defaults to 16px rather than a
// colour or a face), plus --crepe-color-error, which no sheet reads but
// Crepe's own themes declare.
const CREPE_VARIABLES = [
    '--crepe-color-background', '--crepe-color-on-background',
    '--crepe-color-surface', '--crepe-color-surface-low',
    '--crepe-color-on-surface', '--crepe-color-on-surface-variant',
    '--crepe-color-outline', '--crepe-color-primary',
    '--crepe-color-secondary', '--crepe-color-on-secondary',
    '--crepe-color-inverse', '--crepe-color-on-inverse',
    '--crepe-color-inline-code', '--crepe-color-error',
    '--crepe-color-hover', '--crepe-color-selected', '--crepe-color-inline-area',
    '--crepe-color-diff-added', '--crepe-color-diff-removed',
    '--crepe-font-title', '--crepe-font-default', '--crepe-font-code',
    '--crepe-shadow-1', '--crepe-shadow-2',
];

// The skin tokens (the W1 vocabulary) a renderer may read.
const SKIN_TOKENS = new Set([
    '--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2',
    '--ink', '--ink-2', '--ink-3', '--brass', '--brass-soft', '--teal',
    '--teal-soft', '--teal-sheet', '--red', '--red-soft', '--green',
    '--code-bg', '--prose-ink', '--shadow', '--lift', '--font-ui',
    '--font-mono', '--font-prose', '--radius',
]);

type Rule = { selectors: string[]; decls: Map<string, string> };

/** Flat rules (the injected CSS has no at-rules), comments stripped. */
function parse(css: string): Rule[] {
    const out: Rule[] = [];
    const body = css.replace(/\/\*[\s\S]*?\*\//g, '');
    for (const r of body.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const decls = new Map<string, string>();
        for (const d of r[2].split(';')) {
            const i = d.indexOf(':');
            if (i > 0) decls.set(d.slice(0, i).trim(), d.slice(i + 1).trim());
        }
        out.push({ selectors: r[1].split(',').map((s) => s.trim().replace(/\s+/g, ' ')), decls });
    }
    return out;
}

/** The declarations every rule naming `selector` gives it, later rules winning. */
function rule(css: string, selector: string): Record<string, string> {
    const hits = parse(css).filter((r) => r.selectors.includes(selector));
    assert.ok(hits.length > 0, `no rule for ${selector}`);
    return Object.assign({}, ...hits.map((r) => Object.fromEntries(r.decls)));
}

function expectDecls(css: string, selector: string, want: Record<string, string>) {
    const got = rule(css, selector);
    for (const [prop, value] of Object.entries(want)) {
        assert.equal(got[prop], value, `${selector} { ${prop} }`);
    }
}

test('the token map names every --crepe-* variable Crepe reads, each on a skin token (R02)', () => {
    const decls = new Map<string, string>();
    for (const r of parse(m.CREPE_TOKEN_CSS)) for (const [k, v] of r.decls) decls.set(k, v);
    for (const name of CREPE_VARIABLES) {
        const value = decls.get(name);
        assert.ok(value, `${name} is mapped`);
        const token = /^var\((--[a-z0-9-]+)\)$/.exec(value!);
        assert.ok(token, `${name}: ${value} is one var()`);
        assert.ok(SKIN_TOKENS.has(token![1]), `${name} → ${token![1]} is a skin token`);
    }
    assert.deepEqual([...decls.keys()].sort(), [...CREPE_VARIABLES].sort());
    // The semantics: the ground and text are the sheet and prose ink, the
    // selection is brass, an error is red, the faces are the skin's.
    assert.equal(decls.get('--crepe-color-background'), 'var(--sheet)');
    assert.equal(decls.get('--crepe-color-on-background'), 'var(--prose-ink)');
    assert.equal(decls.get('--crepe-color-surface'), 'var(--sheet-2)');
    assert.equal(decls.get('--crepe-color-selected'), 'var(--brass-soft)');
    assert.equal(decls.get('--crepe-color-primary'), 'var(--brass)');
    assert.equal(decls.get('--crepe-color-inline-area'), 'var(--code-bg)');
    assert.equal(decls.get('--crepe-color-error'), 'var(--red)');
    assert.equal(decls.get('--crepe-color-diff-added'), 'var(--green)');
    assert.equal(decls.get('--crepe-color-diff-removed'), 'var(--red)');
    assert.equal(decls.get('--crepe-font-title'), 'var(--font-prose)');
    assert.equal(decls.get('--crepe-font-default'), 'var(--font-ui)');
    assert.equal(decls.get('--crepe-font-code'), 'var(--font-mono)');
    assert.equal(decls.get('--crepe-shadow-1'), 'var(--shadow)');
    // On :root (diff.css declares its two colours on .milkdown itself).
    const where = parse(m.CREPE_TOKEN_CSS).map((r) => r.selectors.join(','));
    assert.deepEqual(where, [':root', ':root .milkdown']);
    // The whole map is part of the injected CSS.
    assert.ok(m.milkdownCss().includes(m.CREPE_TOKEN_CSS));
});

test('the injected CSS has no colour or font literal and reads only skin tokens (R02)', () => {
    const css = m.milkdownCss();
    assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b/i, 'no hex colour');
    assert.doesNotMatch(css, /\b(rgba?|hsla?|oklch|lab|lch)\(/i, 'no functional colour');
    assert.doesNotMatch(css, /(?<![\w-])(white|black|gr[ae]y|silver)(?![\w-])/i, 'no named colour');
    assert.doesNotMatch(css, /Noto|Space Mono|Arial|Helvetica|Cambria|Menlo|monospace|serif/i, 'no font name');
    assert.doesNotMatch(css, /data-theme|data-scheme|\.dark\b/, 'nothing keyed on a scheme or skin id');
    for (const [, name] of css.matchAll(/var\((--[a-z0-9-]+)/g)) {
        // --colmd-zoom and --colmd-font are the style's text size and font.
        assert.ok(SKIN_TOKENS.has(name) || name.startsWith('--crepe-') || name === '--colmd-zoom'
            || name === '--colmd-font', `var(${name}) is a skin token`);
    }
    // The frame paints the sheet, through the Crepe ground a library theme
    // redeclares.
    expectDecls(css, 'html', { background: 'var(--crepe-color-background)' });
    expectDecls(css, '.milkdown', { background: 'var(--crepe-color-background)' });
});

test('the prose carries the mock’s .md values (R02)', () => {
    const css = m.milkdownCss();
    expectDecls(css, '.milkdown > .ProseMirror', {
        padding: '1.625rem 1.875rem 1.875rem',
        'font-family': 'var(--colmd-font, var(--font-prose))',
        color: 'var(--crepe-color-on-background)',
        'font-size': '0.96875rem',
        'line-height': '1.55',
        'font-size-adjust': '0.475',
    });
    expectDecls(css, '.milkdown .ProseMirror h1', {
        'font-size': '1.5rem', 'font-weight': '600', 'letter-spacing': '-0.01em', 'line-height': '1.2',
    });
    expectDecls(css, '.milkdown .ProseMirror h2', {
        'font-family': 'var(--font-ui)', 'font-size': '0.8125rem', 'font-weight': '600',
        'letter-spacing': '0.08em', 'text-transform': 'uppercase', color: 'var(--crepe-color-on-surface-variant)',
        'margin-top': '1.375rem', 'margin-bottom': '0.5rem', 'font-size-adjust': 'none',
    });
    expectDecls(css, '.milkdown .ProseMirror h3', {
        'font-size': '1rem', 'font-weight': '600', 'line-height': '1.3', 'margin-top': '1.125rem',
    });
    // Crepe's fixed-size paragraph is overridden.
    expectDecls(css, '.milkdown .ProseMirror p', {
        'margin-top': '0.625rem', 'font-size': 'inherit', 'line-height': 'inherit', padding: '0',
    });
    expectDecls(css, '.milkdown .ProseMirror ul', { margin: '0.625rem 0 0' });
    expectDecls(css, '.milkdown .ProseMirror .milkdown-list-item-block', { 'margin-top': '0.3125rem' });
    expectDecls(css, '.milkdown .ProseMirror .milkdown-list-item-block li .label-wrapper', {
        width: '1.125rem', color: 'var(--crepe-color-outline)',
    });
    expectDecls(css, '.milkdown .ProseMirror .milkdown-list-item-block li', { gap: '0.375rem' });
    expectDecls(css, '.milkdown .ProseMirror strong', { 'font-weight': '600' });
    expectDecls(css, '.milkdown .ProseMirror a', {
        color: 'inherit', 'text-decoration': 'underline', 'text-decoration-color': 'var(--line-2)',
        'text-decoration-thickness': '1px', 'text-underline-offset': '0.1875rem',
    });
    expectDecls(css, '.milkdown .ProseMirror a:hover', { 'text-decoration-color': 'var(--crepe-color-on-surface-variant)' });
    expectDecls(css, '.milkdown .ProseMirror blockquote', {
        margin: '0.875rem 0 0', padding: '0.0625rem 0 0.0625rem 0.875rem',
        'border-left': '2px solid var(--line-2)', color: 'var(--crepe-color-on-surface-variant)',
    });
    expectDecls(css, '.milkdown .ProseMirror blockquote::before', { content: 'none' });
    expectDecls(css, '.milkdown .ProseMirror blockquote p:first-child', { 'margin-top': '0' });
    expectDecls(css, '.milkdown .ProseMirror code', {
        'font-family': 'var(--font-mono)', 'font-size': '0.8em', padding: '0.0625rem 0.25rem',
        border: '1px solid var(--line)', 'border-radius': '0.25rem', background: 'var(--crepe-color-inline-area)',
        'font-size-adjust': 'none',
    });
    // Crepe colours an inline formula with --crepe-color-primary (brass); it is text.
    expectDecls(css, ".milkdown .ProseMirror span[data-type='math_inline']", { color: 'inherit' });
    for (const sel of ['.milkdown .ProseMirror pre', '.milkdown .ProseMirror .milkdown-code-block']) {
        expectDecls(css, sel, {
            margin: '0.75rem 0 0', border: '1px solid var(--line)', 'border-radius': '0.375rem', 'font-size-adjust': 'none',
        });
    }
    // The shared sheet's ground and ink; the pre fallback reads them through
    // the Crepe variables that resolve to the same tokens under the skin.
    expectDecls(css, '.milkdown .ProseMirror .milkdown-code-block', { background: 'var(--code-bg)', color: 'var(--ink-2)' });
    expectDecls(css, '.milkdown .ProseMirror pre', {
        background: 'var(--crepe-color-inline-area)', color: 'var(--crepe-color-on-surface-variant)',
    });
    expectDecls(css, '.milkdown .ProseMirror pre', {
        padding: '0.625rem 0.75rem', 'font-family': 'var(--font-mono)', 'font-size': '0.71875rem', 'line-height': '1.55',
    });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-scroller', {
        'font-family': 'var(--font-mono)', 'font-size': '0.71875rem', 'line-height': '1.55',
    });
    expectDecls(css, '.milkdown .ProseMirror table', {
        width: '100%', 'border-collapse': 'collapse', 'font-size': '0.92em',
    });
    expectDecls(css, '.milkdown .ProseMirror .milkdown-table-block th', {
        padding: '0.375rem 0.75rem 0.375rem 0', 'border-bottom': '1px solid var(--line-2)', 'text-align': 'left',
        'font-family': 'var(--font-ui)', 'font-size': '0.71875rem', 'font-weight': '600',
        'letter-spacing': '0.06em', 'text-transform': 'uppercase', color: 'var(--crepe-color-on-surface-variant)',
        'font-size-adjust': 'none',
    });
    expectDecls(css, '.milkdown .ProseMirror .milkdown-table-block td', {
        padding: '0.4375rem 0.75rem 0.4375rem 0', 'border-bottom': '1px solid var(--line)', 'vertical-align': 'top',
    });
    expectDecls(css, '.milkdown .ProseMirror hr', {
        margin: '1.375rem 0 0', border: '0', 'border-top': '1px solid var(--line)',
    });
});

test('the caret is teal and the selection brass; zoom, folding and the hidden handle stay (R02)', () => {
    const css = m.milkdownCss();
    expectDecls(css, '.milkdown .ProseMirror', {
        'caret-color': 'var(--teal) !important', zoom: 'var(--colmd-zoom, 1)',
    });
    expectDecls(css, '.milkdown .milkdown-block-handle', { display: 'none !important' });
    for (const sel of ['.milkdown .ProseMirror .colmd-fold-toggle', '.milkdown .ProseMirror .colmd-fold-ellipsis',
        '.milkdown .ProseMirror .colmd-foldable.colmd-folded > .colmd-fold-toggle::before']) {
        rule(css, sel);
    }
    expectDecls(css, '.milkdown .milkdown-code-block .cm-cursor', { 'border-left-color': 'var(--teal)' });
});

test('Crepe’s floating chrome is a sheet-2 plate with a line-2 edge in the UI face (R02)', () => {
    const css = m.milkdownCss();
    for (const sel of [
        '.milkdown .milkdown-toolbar',
        '.milkdown .milkdown-slash-menu',
        '.milkdown .milkdown-link-preview > .link-preview',
        '.milkdown .milkdown-link-edit > .link-edit',
        '.milkdown .milkdown-code-block .list-wrapper',
        '.milkdown .milkdown-table-block .cell-handle .button-group',
    ]) {
        expectDecls(css, sel, {
            background: 'var(--crepe-color-surface)', color: 'var(--crepe-color-on-surface)',
            border: '1px solid var(--line-2)',
            'border-radius': 'var(--radius)', 'box-shadow': 'var(--crepe-shadow-1)',
            'font-family': 'var(--font-ui)', 'font-size-adjust': 'none',
        });
    }
    // An active mark is an on-state (teal); the chosen slash row is the
    // selection (brass, through Crepe's primary).
    expectDecls(css, '.milkdown .milkdown-toolbar .toolbar-item.active svg', { color: 'var(--teal)' });
    expectDecls(css, '.milkdown .milkdown-slash-menu .menu-groups .menu-group li.active svg', { color: 'var(--crepe-color-primary)' });
    expectDecls(css, '.milkdown .milkdown-slash-menu .menu-groups .menu-group li', { height: '1.875rem' });
});

test('the skin CSS follows Crepe’s sheets, precedes ready, and the frame keeps the host’s data-theme (R02)', () => {
    const sheets = SOURCE.indexOf('api.injectCss(await crepeThemeCss());');
    const skin = SOURCE.indexOf('api.injectCss(milkdownCss());');
    assert.ok(sheets > 0 && skin > sheets, 'milkdownCss after the Crepe sheets');
    assert.ok(skin < SOURCE.indexOf('api.ready()'), 'and before ready');
    assert.doesNotMatch(SOURCE, /setAttribute\(\s*['"]data-theme/, 'data-theme is the host’s skin id');
    assert.match(SOURCE, /colorScheme = theme\.scheme/);
    // No shipped palette or unloaded face left in the source either.
    assert.doesNotMatch(SOURCE, /#ffffff|#1a1a1a|Noto Serif|Noto Sans|Space Mono/);
});

// ---------------------------------------------------------------------------
// Fenced code (R02 W2b)
// ---------------------------------------------------------------------------

// The `tags` namespace of @lezer/highlight 1.2.5 (the version Crepe's graph
// resolves today): its tags and its modifiers. The model gives each tag a
// distinct object and each modifier a function over one, so an entry naming
// a tag Lezer does not have resolves to nothing and fails.
const LEZER_TAGS = [
    'comment', 'lineComment', 'blockComment', 'docComment', 'name', 'variableName', 'typeName',
    'tagName', 'propertyName', 'attributeName', 'className', 'labelName', 'namespace', 'macroName',
    'literal', 'string', 'docString', 'character', 'attributeValue', 'number', 'integer', 'float',
    'bool', 'regexp', 'escape', 'color', 'url', 'keyword', 'self', 'null', 'atom', 'unit', 'modifier',
    'operatorKeyword', 'controlKeyword', 'definitionKeyword', 'moduleKeyword', 'operator',
    'derefOperator', 'arithmeticOperator', 'logicOperator', 'bitwiseOperator', 'compareOperator',
    'updateOperator', 'definitionOperator', 'typeOperator', 'controlOperator', 'punctuation',
    'separator', 'bracket', 'angleBracket', 'squareBracket', 'paren', 'brace', 'content', 'heading',
    'heading1', 'heading2', 'heading3', 'heading4', 'heading5', 'heading6', 'contentSeparator', 'list',
    'quote', 'emphasis', 'strong', 'link', 'monospace', 'strikethrough', 'inserted', 'deleted',
    'changed', 'invalid', 'meta', 'documentMeta', 'annotation', 'processingInstruction',
];
const LEZER_MODIFIERS = ['definition', 'constant', 'function', 'standard', 'local', 'special'];

function lezerModel(): Record<string, any> {
    const ns: Record<string, any> = {};
    for (const name of LEZER_TAGS) ns[name] = Object.freeze({ tag: name });
    for (const mod of LEZER_MODIFIERS) ns[mod] = (inner: any) => Object.freeze({ mod, inner });
    return ns;
}

/** How the model renders a resolved tag, to compare with an entry's text. */
function tagText(t: any): string {
    return t.mod ? `${t.mod}(${tagText(t.inner)})` : t.tag;
}

// The tags the brief names and the role each is drawn in.
const REQUIRED: Record<string, string> = {
    'comment': 'comment',
    'keyword': 'keyword',
    'string': 'string',
    'number': 'number',
    'typeName': 'type',
    'function(variableName)': 'function',
    'variableName': 'variable',
    'operator': 'operator',
    'punctuation': 'punctuation',
    'tagName': 'tag',
    'attributeName': 'attribute',
    'regexp': 'regexp',
    'invalid': 'invalid',
};

// The built-in Night and Paper skins, as the host sends them.
const NIGHT = {
    themeId: 'night', scheme: 'dark', known: true,
    vars: {
        '--canvas': '#151311', '--sheet': '#1c1a17', '--sheet-2': '#23201c', '--sheet-3': '#2a2621',
        '--line': '#312d27', '--line-2': '#3f3931',
        '--ink': '#e2ddd4', '--ink-2': '#a39d92', '--ink-3': '#6f695f',
        '--brass': '#c69d64', '--brass-soft': 'rgba(198, 157, 100, 0.16)',
        '--teal': '#7fb0ab', '--teal-soft': 'rgba(127, 176, 171, 0.14)', '--teal-sheet': '#1a211f',
        '--red': '#cf8b7c', '--red-soft': 'rgba(207, 139, 124, 0.16)', '--green': '#90b893',
        '--code-bg': '#121008', '--prose-ink': '#e9e4da', '--lift': 'rgba(0, 0, 0, 0.8)',
    },
    fonts: { ui: 'Inter', mono: 'Geist Mono', prose: 'Alegreya' },
};
const PAPER = {
    themeId: 'paper', scheme: 'light', known: true,
    vars: {
        '--canvas': '#e6e4de', '--sheet': '#f8f7f3', '--sheet-2': '#f1efea', '--sheet-3': '#e9e7e1',
        '--line': '#d4d0c7', '--line-2': '#c4bfb5',
        '--ink': '#1e2227', '--ink-2': '#5d646b', '--ink-3': '#9a9fa4',
        '--brass': '#8a6d3b', '--brass-soft': 'rgba(138, 109, 59, 0.12)',
        '--teal': '#2e7773', '--teal-soft': 'rgba(46, 119, 115, 0.10)', '--teal-sheet': '#f1f5f3',
        '--red': '#a3463a', '--red-soft': 'rgba(163, 70, 58, 0.10)', '--green': '#3d7750',
        '--code-bg': '#efede7', '--prose-ink': '#1a1e22', '--lift': 'rgba(40, 36, 28, 0.22)',
    },
    fonts: { ui: 'Inter', mono: 'Geist Mono', prose: 'Alegreya' },
};

test('the fenced-code highlight maps the Lezer tags onto palette roles and names no colour of its own (R02 W2b)', () => {
    const entries = m.LEZER_TAG_ROLES;
    const byTag = new Map(entries.map((e) => [e.tag, e.role]));
    assert.equal(byTag.size, entries.length, 'each tag listed once');
    for (const [tag, role] of Object.entries(REQUIRED)) {
        assert.equal(byTag.get(tag), role, `${tag} is drawn in ${role}`);
    }
    const roles = new Set<string>(SYNTAX_ROLES);
    const ns = lezerModel();
    for (const { tag, role } of entries) {
        assert.ok(roles.has(role), `${tag}: ${role} is a syntaxPalette role`);
        const resolved = m.lezerTag(ns, tag);
        assert.ok(resolved, `${tag} is a @lezer/highlight tag`);
        assert.equal(tagText(resolved), tag);
    }
    assert.equal(m.lezerTag(ns, 'notATag'), null);
    assert.equal(m.lezerTag(ns, 'notAModifier(variableName)'), null);

    // Every spec's colour is its role's property; nothing else colours code.
    const specs = m.highlightSpecs(ns);
    assert.equal(specs.length, entries.length, 'every entry becomes a spec');
    specs.forEach((spec, i) => {
        assert.equal(tagText(spec.tag), entries[i].tag);
        assert.equal(spec.color, `var(${m.syntaxVar(entries[i].role)})`);
        assert.equal(m.syntaxVar(entries[i].role), `--colmd-syntax-${entries[i].role}`);
        for (const key of Object.keys(spec)) {
            assert.ok(['tag', 'color', 'fontStyle', 'fontWeight', 'textDecoration'].includes(key), `${entries[i].tag}: ${key}`);
        }
    });
    const text = JSON.stringify(specs);
    assert.doesNotMatch(text, /#[0-9a-f]{3,8}\b/i, 'no hex colour');
    assert.doesNotMatch(text, /\b(rgba?|hsla?|oklch|lab|lch)\(/i, 'no functional colour');
    assert.doesNotMatch(text, /"(white|black|gr[ae]y|silver|red|green|blue)"/i, 'no named colour');
    // A tag Lezer lacks is left out, not an error that would stop the editor.
    const partial = lezerModel();
    delete partial.regexp;
    assert.equal(m.highlightSpecs(partial).length, entries.length - 1);
});

test('the skin record fills the highlight’s properties from syntaxPalette, and none before it (R02 W2b)', () => {
    for (const skin of [NIGHT, PAPER]) {
        const palette = syntaxPalette(skin as any);
        assert.ok(palette, `${skin.themeId} has a palette`);
        const vars = m.syntaxVars(palette);
        assert.deepEqual(Object.keys(vars), SYNTAX_ROLES.map((r) => `--colmd-syntax-${r}`));
        for (const role of SYNTAX_ROLES) {
            assert.equal(vars[m.syntaxVar(role)], palette![role], `${skin.themeId} ${role}`);
            assert.match(vars[m.syntaxVar(role)]!, /^#[0-9a-f]{6}$/);
        }
        // Every role the highlight reads is written.
        for (const { role } of m.LEZER_TAG_ROLES) assert.ok(vars[m.syntaxVar(role)]);
    }
    // The skins draw code differently: the colours are the skin's.
    assert.notDeepEqual(m.syntaxVars(syntaxPalette(NIGHT as any)), m.syntaxVars(syntaxPalette(PAPER as any)));
    // No palette (the placeholder): every property is removed, so code
    // inherits the block's ink.
    const none = m.syntaxVars(null);
    assert.deepEqual(Object.values(none), SYNTAX_ROLES.map(() => null));
    assert.equal(syntaxPalette({ themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } } as any), null);
});

test('the highlight reaches Crepe’s own CodeMirror, with One Dark left out and the theme followed (R02 W2b)', () => {
    // Crepe's code-mirror feature: no default theme, the skin's highlight.
    assert.match(SOURCE,
        /featureConfigs\[\(Crepe\.Feature && Crepe\.Feature\.CodeMirror\) \|\| 'code-mirror'\] = \{\s*theme: null,\s*extensions: \[syntaxHighlighting\(skinHighlightStyle\(\)\)\],\s*\};/);
    assert.match(SOURCE, /HighlightStyle\.define\(highlightSpecs\(tags\)\)/);
    // The graph's own specifiers, no deps= (which would build a second instance).
    const imports = [...SOURCE.matchAll(/^import .* from '(https:\/\/esm\.sh\/@(?:codemirror|lezer)\/[^']+)';$/gm)].map((x) => x[1]);
    assert.deepEqual(imports, [
        'https://esm.sh/@codemirror/language@^6.0.0?target=es2022',
        'https://esm.sh/@lezer/highlight@^1.0.0?target=es2022',
    ]);
    // Every skin record rewrites the properties.
    assert.match(SOURCE, /onThemeChange\(applyTheme\)/);
    assert.match(SOURCE, /syntaxVars\(syntaxPalette\(theme as any\)\)/);
    assert.match(SOURCE, /root\.style\.setProperty\(name, value\)/);
    assert.match(SOURCE, /root\.style\.removeProperty\(name\)/);
    // The W2 monochrome override is gone; the block keeps its ground and edge.
    const css = m.milkdownCss();
    assert.doesNotMatch(css, /\.cm-line span/);
    expectDecls(css, '.milkdown .ProseMirror .milkdown-code-block', {
        background: 'var(--code-bg)', border: '1px solid var(--line)',
    });
    // One Dark's surfaces it no longer paints are on the tokens.
    expectDecls(css, '.milkdown .milkdown-code-block .cm-foldPlaceholder', {
        background: 'var(--sheet-3)', border: '1px solid var(--line-2)', color: 'var(--ink-3)',
    });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-specialChar', { color: 'var(--red)' });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-tooltip-autocomplete ul li[aria-selected]', {
        background: 'var(--brass-soft)', color: 'var(--ink)',
    });
    // Without One Dark nothing sets EditorView.darkTheme, so CodeMirror's
    // light base theme applies in every skin: each of its colour rules a
    // code block reaches is restated on the tokens.
    expectDecls(css, '.milkdown .milkdown-code-block .cm-snippetField', { background: 'var(--teal-soft)' });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-snippetFieldPosition', { 'border-left-color': 'var(--ink-3)' });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-tooltip-section:not(:first-child)', { 'border-top-color': 'var(--line-2)' });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-tooltip.cm-tooltip-autocomplete > ul > completion-section', {
        'border-bottom-color': 'var(--line-2)',
    });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-tooltip-autocomplete-disabled ul li[aria-selected]', {
        background: 'var(--sheet-3)', color: 'var(--ink-2)',
    });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-textfield', {
        border: '1px solid var(--line-2)', background: 'var(--sheet-3)', color: 'var(--ink)',
    });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-textfield:focus', { outline: '1px solid var(--teal)' });
    expectDecls(css, '.milkdown .milkdown-code-block .cm-button', {
        'background-image': 'none', 'border-color': 'var(--line-2)',
    });
    // The disabled list is also a .cm-tooltip-autocomplete: its rule comes later.
    const order = parse(css).map((r) => r.selectors.join(','));
    assert.ok(order.indexOf('.milkdown .milkdown-code-block .cm-tooltip-autocomplete-disabled ul li[aria-selected]')
        > order.indexOf('.milkdown .milkdown-code-block .cm-tooltip-autocomplete ul li[aria-selected]'));
    // Every colour rule of the light base theme (the `&light` rules of
    // @codemirror/view 6.43, search 6.7 and autocomplete 6.20, which Crepe's
    // CodeMirror graph resolves today) and every unscoped base rule with a
    // colour literal a block reaches, with the rule that restates it under
    // .milkdown .milkdown-code-block (two classes above the base theme's
    // `.ͼ1`/`.ͼ2` scope). A panel's -top/-bottom element is also .cm-panels.
    const base = '.milkdown .milkdown-code-block ';
    const LIGHT: Array<[string, string, string, Record<string, string>]> = [
        ['view', '.cm-content', '.cm-content', { 'caret-color': 'var(--teal)' }],
        ['view', '.cm-selectionBackground', '.cm-selectionBackground', { background: 'var(--brass-soft) !important' }],
        ['view', '.cm-activeLine', '.cm-activeLine', { background: 'transparent' }],
        ['view', '.cm-specialChar', '.cm-specialChar', { color: 'var(--red)' }],
        ['view', '.cm-gutters', '.cm-gutters', { background: 'transparent', color: 'var(--ink-3)', 'border-right': '0' }],
        ['view', '.cm-activeLineGutter', '.cm-activeLineGutter', { background: 'transparent' }],
        ['view', '.cm-panels', '.cm-panels', { background: 'var(--sheet-2)', color: 'var(--ink)', 'border-color': 'var(--line-2)' }],
        ['view', '.cm-panels-top', '.cm-panels', { 'border-color': 'var(--line-2)' }],
        ['view', '.cm-panels-bottom', '.cm-panels', { 'border-color': 'var(--line-2)' }],
        ['view', '.cm-button', '.cm-button', { 'background-image': 'none', 'border-color': 'var(--line-2)' }],
        ['view', '.cm-textfield', '.cm-textfield', { background: 'var(--sheet-3)' }],
        ['view', '.cm-tooltip', '.cm-tooltip', { background: 'var(--sheet-2)', 'border-color': 'var(--line-2)' }],
        ['view', '.cm-tooltip-section:not(:first-child)', '.cm-tooltip-section:not(:first-child)', { 'border-top-color': 'var(--line-2)' }],
        ['search', '.cm-searchMatch', '.cm-searchMatch', { background: 'var(--brass-soft)' }],
        ['search', '.cm-searchMatch-selected', '.cm-searchMatch.cm-searchMatch-selected', { background: 'var(--teal-soft)' }],
        ['autocomplete', '.cm-tooltip-autocomplete ul li[aria-selected]', '.cm-tooltip-autocomplete ul li[aria-selected]',
            { background: 'var(--brass-soft)', color: 'var(--ink)' }],
        ['autocomplete', '.cm-tooltip-autocomplete-disabled ul li[aria-selected]', '.cm-tooltip-autocomplete-disabled ul li[aria-selected]',
            { background: 'var(--sheet-3)', color: 'var(--ink-2)' }],
        ['autocomplete', '.cm-snippetField', '.cm-snippetField', { background: 'var(--teal-soft)' }],
        // Unscoped base rules with a literal: the caret (black), the search
        // fields (silver), a completion section (silver), a snippet stop
        // (#888), a selection match (#99ff7780), a fold placeholder (#eee).
        ['view', '.cm-cursor', '.cm-cursor', { 'border-left-color': 'var(--teal)' }],
        ['view', '.cm-dropCursor', '.cm-dropCursor', { 'border-left-color': 'var(--teal)' }],
        ['view', '.cm-textfield', '.cm-textfield', { border: '1px solid var(--line-2)', color: 'var(--ink)' }],
        ['autocomplete', '.cm-tooltip.cm-tooltip-autocomplete > ul > completion-section',
            '.cm-tooltip.cm-tooltip-autocomplete > ul > completion-section', { 'border-bottom-color': 'var(--line-2)' }],
        ['autocomplete', '.cm-snippetFieldPosition', '.cm-snippetFieldPosition', { 'border-left-color': 'var(--ink-3)' }],
        ['search', '.cm-selectionMatch', '.cm-selectionMatch', { background: 'var(--teal-soft)' }],
        ['language', '.cm-foldPlaceholder', '.cm-foldPlaceholder', { background: 'var(--sheet-3)', color: 'var(--ink-3)' }],
    ];
    for (const [pkg, library, ours, want] of LIGHT) {
        // The restating rule is the library's own selector, or (a panel's
        // side, the selected match) a selector the same element matches.
        const same = ours === library || (library.startsWith('.cm-panels-') && ours === '.cm-panels')
            || ours === `.cm-searchMatch${library}`;
        assert.ok(same, `@codemirror/${pkg} ${library} is restated by ${ours}`);
        expectDecls(css, base + ours, want);
    }
    for (const required of ['.cm-snippetField', '.cm-tooltip-section', '.cm-tooltip-autocomplete-disabled', '.cm-textfield']) {
        assert.ok(LIGHT.some(([, library]) => library.startsWith(required)), `${required} is listed`);
    }
    // The block's CSS is the shared module's, inside the prose sheet.
    assert.ok(css.includes(m.CODE_BLOCK_CSS));
    assert.match(SOURCE, /const PROSE_CSS = `[\s\S]*\n\$\{CODE_BLOCK_CSS\}\n[\s\S]*`;/);
    // Its library-theme layer (CODE_BLOCK_CREPE_CSS) follows it.
    assert.match(SOURCE, /\n\$\{CODE_BLOCK_CSS\}\n\$\{CODE_BLOCK_CREPE_CSS\}\n/);
    const outside = css.replace(m.CODE_BLOCK_CSS, '').replace(m.CODE_BLOCK_CREPE_CSS, '');
    assert.doesNotMatch(outside, /\.cm-[\w-]+/, 'no CodeMirror rule outside CODE_BLOCK_CSS and its library-theme layer');
});

// ---------------------------------------------------------------------------
// Insert, and no uploads (§3b)
// ---------------------------------------------------------------------------

/** A stand-in click on an <a href> (node has no DOM): `closest` answers
 *  `a[href]` with the anchor, and `.milkdown-link-preview` only for the
 *  address in Crepe's link tooltip. */
function linkClick(href: string, opts: { tooltip?: boolean; metaKey?: boolean; ctrlKey?: boolean } = {}) {
    const anchor = {
        getAttribute: (name: string) => (name === 'href' ? href : null),
        closest: (sel: string) => (sel === '.milkdown-link-preview' && opts.tooltip ? {} : null),
    };
    const e = {
        target: { closest: (sel: string) => (sel === 'a[href]' ? anchor : null) },
        metaKey: !!opts.metaKey, ctrlKey: !!opts.ctrlKey,
        defaultPrevented: false, propagationStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.propagationStopped = true; },
    };
    return e;
}

test('a link to a cell file opens it in a new tab: the tooltip’s address, Cmd/Ctrl-click, or a read-only click (§3b)', () => {
    const follow = (e: ReturnType<typeof linkClick>, readOnly = false) => {
        const calls: Array<[string, { newTab: boolean }]> = [];
        const followed = m.followLinkClick(e, 'notes/doc.md', readOnly, (p, o) => calls.push([p, o]));
        return { followed, calls, prevented: e.defaultPrevented, stopped: e.propagationStopped };
    };
    const opened = { followed: true, calls: [['notes/data.csv', { newTab: true }]], prevented: true, stopped: true };
    // The address in Crepe's link tooltip, a plain click while editable: its
    // target=_blank (a popup the sandbox blocks) never runs.
    assert.deepEqual(follow(linkClick('data.csv', { tooltip: true })), opened);
    // A link in the text: Cmd- or Ctrl-click while editable.
    assert.deepEqual(follow(linkClick('data.csv', { metaKey: true })), opened);
    assert.deepEqual(follow(linkClick('data.csv', { ctrlKey: true })), opened);
    // A plain click while read-only.
    assert.deepEqual(follow(linkClick('data.csv'), true), opened);
    // A plain click on a link in the text while editable places the caret.
    assert.deepEqual(follow(linkClick('data.csv')), { followed: false, calls: [], prevented: false, stopped: false });
    // An external URL is never opened, from the tooltip or the text; while
    // read-only its default is still prevented, so the frame never navigates.
    const none = { followed: false, calls: [] };
    for (const href of ['https://example.com/x', '#top', '//cdn.example.com/a.js', '../../escape.csv']) {
        assert.deepEqual(follow(linkClick(href, { tooltip: true })), { ...none, prevented: false, stopped: false }, href);
        assert.deepEqual(follow(linkClick(href, { metaKey: true })), { ...none, prevented: false, stopped: false }, href);
        assert.deepEqual(follow(linkClick(href), true), { ...none, prevented: true, stopped: false }, href);
    }
    // A click off any link does nothing.
    const off = { target: { closest: () => null }, preventDefault: () => assert.fail('prevented'), stopPropagation: () => assert.fail('stopped') };
    assert.equal(m.followLinkClick(off, 'doc.md', true, () => assert.fail('revealed')), false);
    // The editor column's click listener routes every click through it.
    assert.match(SOURCE, /followLinkClick\(e, filename, isReadOnly, \(cellPath, options\) => api\.revealFile\(cellPath, options\)\);/);
});

test('Insert writes a document-relative href that resolveCellPath reads back to the file', () => {
    assert.equal(m.relativeHref('notes.md', 'plot.png'), 'plot.png');
    assert.equal(m.relativeHref('notes.md', 'charts/plot.png'), 'charts/plot.png');
    assert.equal(m.relativeHref('docs/notes.md', 'charts/plot.png'), '../charts/plot.png');
    assert.equal(m.relativeHref('docs/x.md', 'docs/y.csv'), 'y.csv');
    assert.equal(m.relativeHref('a/b/x.md', 'a/c/y.png'), '../c/y.png');
    assert.equal(m.relativeHref('a/b/x.md', 'z.txt'), '../../z.txt');
    assert.equal(m.relativeHref('docs/x.md', 'docs'), '../docs');
    // Each segment is encoded, so a space, '#', '?' or '%' survives the trip.
    assert.equal(m.relativeHref('a.md', 'charts/p q.png'), 'charts/p%20q.png');
    assert.equal(m.relativeHref('a.md', 'r#1?.csv'), 'r%231%3F.csv');
    const pairs: Array<[string, string]> = [
        ['notes.md', 'plot.png'], ['notes.md', 'charts/plot.png'], ['docs/notes.md', 'charts/plot.png'],
        ['docs/x.md', 'docs/y.csv'], ['a/b/x.md', 'a/c/y.png'], ['a/b/x.md', 'z.txt'],
        ['a.md', 'charts/p q.png'], ['a.md', 'r#1?.csv'], ['a.md', '100%.txt'], ['d/e/f.md', 'd/e/g/h i.svg'],
    ];
    for (const [md, path] of pairs) {
        assert.equal(m.resolveCellPath(md, m.relativeHref(md, path)), path, `${md} → ${path}`);
    }
});

test('Insert writes an image file as an image and every other file as a link, named by its basename', () => {
    for (const ext of ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico', 'PNG', 'JpG']) {
        assert.equal(m.insertionFor('notes.md', `charts/plot.${ext}`).kind, 'image', ext);
    }
    for (const path of ['results.csv', 'data/table.json', 'report.pdf', 'notes.txt', 'png', 'charts/plot.png.bak']) {
        assert.equal(m.insertionFor('notes.md', path).kind, 'link', path);
    }
    assert.deepEqual(m.insertionFor('docs/notes.md', 'charts/plot.png'),
        { kind: 'image', href: '../charts/plot.png', label: 'plot.png' });
    assert.deepEqual(m.insertionFor('notes.md', 'results.csv'),
        { kind: 'link', href: 'results.csv', label: 'results.csv' });
});

test('Insert lists the cell’s files but the document itself, once each, sorted', () => {
    assert.deepEqual(
        m.insertCandidates(['notes.md', 'b.csv', 'charts/plot.png', 'a.txt', 'b.csv', ''], 'notes.md'),
        ['a.txt', 'b.csv', 'charts/plot.png']);
    assert.deepEqual(m.insertCandidates(['notes.md'], 'notes.md'), []);
    // Only the exact document is left out, never a namesake elsewhere.
    assert.deepEqual(m.insertCandidates(['docs/notes.md', 'notes.md'], 'docs/notes.md'), ['notes.md']);
    // An empty folder's hidden keep marker is not a file to insert; a file
    // merely named like it is.
    assert.deepEqual(
        m.insertCandidates(['notes.md', 'empty/.collkeep', '.collkeep', 'a/b/.collkeep', 'x.collkeep', 'plot.png'], 'notes.md'),
        ['plot.png', 'x.collkeep']);
});

test('the Insert list opens at the caret and stays inside the editor column, however narrow the cell (§3b, RS-03)', () => {
    const box = (left: number, top: number, right: number, bottom: number): Box => ({ left, top, right, bottom });
    const { INSERT_LIST_WIDTH: W, INSERT_LIST_MARGIN: M, INSERT_LIST_GAP: G, INSERT_LIST_MIN_HEIGHT: H } = m;
    assert.equal(W, 300);
    assert.equal(H, 160);
    // Both edges of a placed list, in viewport coordinates.
    const edges = (anchor: Box, column: Box) => {
        const p = m.placeInsertList(anchor, column, W);
        return { ...p, left: column.left + p.left, right: column.left + p.left + p.width, top: column.top + p.top };
    };
    // A wide cell: below the caret, its left edge at the caret, at its own width.
    const caret = box(100, 50, 101, 70);
    const wide = edges(caret, box(0, 0, 900, 600));
    assert.deepEqual([wide.left, wide.right, wide.width, wide.top, wide.above], [100, 400, W, 70 + G, false]);
    assert.equal(wide.maxHeight, 600 - M - (70 + G));
    // A caret near the column's right edge: the list moves left to stay inside.
    const right = edges(box(800, 50, 801, 70), box(0, 0, 900, 600));
    assert.deepEqual([right.left, right.right], [900 - M - W, 900 - M]);
    // A 300 px cell: the list spans the column less the margins.
    const narrow = edges(box(250, 40, 251, 60), box(0, 0, 300, 400));
    assert.equal(narrow.width, 300 - 2 * M);
    assert.deepEqual([narrow.left, narrow.right], [M, 300 - M]);
    // Relative to the column's box, the list's positioned ancestor.
    const offsetColumn = box(200, 40, 620, 500);
    const offset = m.placeInsertList(box(260, 90, 261, 110), offsetColumn, W);
    assert.deepEqual([offset.left, offset.top], [60, 110 + G - 40]);
    assert.equal(offset.width, W);
    // A caret low in the column opens the list above it when there is more
    // room there: `top` is then the list's bottom edge, a gap above the caret.
    const low = m.placeInsertList(box(100, 560, 101, 580), box(0, 0, 900, 600), W);
    assert.equal(low.above, true);
    assert.equal(low.top, 560 - G);
    assert.equal(low.maxHeight, 560 - G - M);
    // Low, but in a column too short for either: the larger side wins.
    const short = m.placeInsertList(box(100, 20, 101, 40), box(0, 0, 900, 120), W);
    assert.equal(short.above, false);
    // Never a negative size, however small the column.
    const tiny = m.placeInsertList(box(2, 8, 3, 28), box(0, 0, 12, 30), W);
    assert.equal(tiny.width, 0);
    assert.equal(tiny.maxHeight, 0);
    // The source: the anchor is the caret's box, the column the editor root;
    // a list above the caret lifts itself onto its bottom edge.
    assert.match(SOURCE, /const c = view\.coordsAtPos\(view\.state\.selection\.from\);/);
    assert.match(SOURCE, /getAnchor=\{caretBox\}/);
    assert.match(SOURCE, /columnRef=\{containerRef\}/);
    assert.match(SOURCE, /left: place\.left, top: place\.top, width: place\.width, maxHeight: place\.maxHeight,\s*transform: place\.above \? 'translateY\(-100%\)' : 'none'/);
    assert.match(SOURCE, /<div role="listbox" style=\{\{ flex: '0 1 auto', minHeight: 0, maxHeight: '17\.5rem', overflowY: 'auto' \}\}>/);
    assert.doesNotMatch(SOURCE, /100vw/);
});

/** Run a paste filter against a stand-in `document` (node has no DOM) whose
 *  template models only what the filter reads: the <img> tags of well-formed
 *  test markup and their src, whether an element carries data-pm-slice,
 *  removing an <img>, and serializing what is left. Parsing real clipboard
 *  HTML is the browser's own (the filter hands it to an inert template). */
function stripWithStandIn(strip: (html: string) => string, html: string): string {
    const g = globalThis as any;
    const prior = g.document;
    g.document = {
        createElement(tag: string) {
            assert.equal(tag, 'template');
            let markup = '';
            const removed = new Set<number>();
            const IMG = /<img\b[^>]*>/gi;
            return {
                set innerHTML(value: string) { markup = value; removed.clear(); },
                get innerHTML() { return markup.replace(IMG, (img: string, at: number) => (removed.has(at) ? '' : img)); },
                content: {
                    querySelector(selector: string) {
                        assert.equal(selector, '[data-pm-slice]');
                        return /<[a-z][^>]*\sdata-pm-slice=/i.test(markup) ? {} : null;
                    },
                    querySelectorAll(selector: string) {
                        assert.equal(selector, 'img');
                        return [...markup.matchAll(IMG)].map((hit) => ({
                            getAttribute(name: string) {
                                const a = new RegExp(`\\s${name}=(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, 'i').exec(hit[0]);
                                return a ? (a[1] ?? a[2] ?? a[3]) : null;
                            },
                            remove() { removed.add(hit.index!); },
                        }));
                    },
                },
            };
        },
    };
    try {
        return strip(html);
    } finally {
        if (prior === undefined) delete g.document; else g.document = prior;
    }
}

test('pasted or dropped HTML keeps the images that are references and only those (§3b)', () => {
    const strip = (html: string) => stripWithStandIn(m.stripPastedImages, html);
    // From a web page, an office app, a file manager: a web, data: or blob:
    // image (or one with no src) is removed, and nothing else.
    assert.equal(strip('<p>a<img src="https://example.com/p.png" alt="p">b</p>'), '<p>ab</p>');
    assert.equal(strip('<img src="data:image/png;base64,AAA"><img src=\'blob:https://app/1\'>tail'), 'tail');
    assert.equal(strip('<p><img src="//cdn.example/x.png"><img src="file:///home/x.png"><img alt="none"><img src="#x"></p>'), '<p></p>');
    assert.equal(strip('<p><img src="  HTTPS://example.com/x.png "> x</p>'), '<p> x</p>');
    // A relative path (a file of the cell, as the document's own images are)
    // is a reference and stays.
    const relative = '<p><img src="charts/plot.png" alt="plot"> and <img src="../a%20b.png"> and <img src=/root.png></p>';
    assert.equal(strip(relative), relative);
    assert.equal(strip('<p><img src="charts/plot.png"><img src="https://example.com/x.png"></p>'),
        '<p><img src="charts/plot.png"></p>');
    // HTML a ProseMirror editor copied (this document, another one, a
    // comment) is a document's own content: copy, cut and paste keep every
    // image, the web and data: ones it already holds too, all but a blob: one.
    const block = '<img src="charts/plot.png" data-type="image-block" data-pm-slice="0 0 []">';
    assert.equal(strip(block), block);
    assert.equal(
        strip('<p data-pm-slice="1 1 []">intro <img src="charts/plot.png" alt="plot"> <img src="https://example.com/x.png"> <img src="data:image/png;base64,AA"> <img src="blob:https://app/1"></p>'),
        '<p data-pm-slice="1 1 []">intro <img src="charts/plot.png" alt="plot"> <img src="https://example.com/x.png"> <img src="data:image/png;base64,AA"> </p>');
    // Markup without an image is never touched.
    const plain = '<h1 id="imgx">Title</h1><p><a href="img.png">img</a></p>';
    assert.equal(strip(plain), plain);
});

test('every upload path is gone: no Image slash item, no Upload control, file paste and drop insert nothing (§3b)', () => {
    // Crepe's image feature: every upload rejects, the link field's text
    // says what it takes.
    const imageBlock = /featureConfigs\[\(Crepe\.Feature && Crepe\.Feature\.ImageBlock\) \|\| 'image-block'\] = \{([\s\S]*?)\n            \};/.exec(SOURCE);
    assert.ok(imageBlock, 'the image feature is configured');
    assert.match(imageBlock![1], /proxyDomURL: \(url\) =>/);
    assert.match(imageBlock![1], /onUpload: \(\) => Promise\.reject\(new Error\(/);
    assert.match(imageBlock![1], /blockUploadPlaceholderText: 'Paste a link or a path in this cell'/);
    assert.match(imageBlock![1], /inlineUploadPlaceholderText: 'Link or path'/);
    // The slash menu: no Image item, and the one Insert item clears the
    // typed "/…" and opens the file list.
    const blockEdit = /featureConfigs\[\(Crepe\.Feature && Crepe\.Feature\.BlockEdit\) \|\| 'block-edit'\] = \{([\s\S]*?)\n            \};/.exec(SOURCE);
    assert.ok(blockEdit, 'the block-edit feature is configured');
    assert.match(blockEdit![1], /advancedGroup: \{ image: null \},/);
    assert.match(blockEdit![1],
        /buildMenu: \(builder\) => \{\s*builder\.getGroup\('advanced'\)\.addItem\('insert-file', \{\s*label: 'Insert file…',\s*icon: INSERT_FILE_ICON,\s*onRun: \(ctx\) => \{\s*ctx\.get\(commandsCtx\)\.call\(clearTextInCurrentBlockCommand\.key\);\s*setInsertOpen\(true\);/);
    // Paste and drop, configured after Crepe's builder (configs run in the
    // order they were added): a file inserts nothing, HTML keeps only the
    // images that are references.
    const crepe = SOURCE.indexOf('const c = new Crepe(');
    const override = SOURCE.indexOf('ctx.update(uploadConfig.key, (prev) => ({ ...prev, uploader: async () => [] }));');
    assert.ok(crepe > 0 && override > crepe, 'the uploader override follows new Crepe(');
    assert.ok(SOURCE.indexOf('c.editor.config((ctx) => {', crepe) < override);
    assert.ok(SOURCE.includes('ctx.update(editorViewOptionsCtx, (prev) => ({ ...prev, transformPastedHTML: stripPastedImages }));'));
    // An existing empty image keeps its link field but has no Upload control.
    const css = m.milkdownCss();
    expectDecls(css, '.milkdown .milkdown-image-block .image-edit .placeholder .uploader', { display: 'none !important' });
    expectDecls(css, '.milkdown .milkdown-image-inline .image-edit .placeholder .uploader', { display: 'none !important' });
    // Nothing can write bytes or a blob: URL.
    assert.doesNotMatch(SOURCE, /createObjectURL|stageDraftFile|saveSiblingFile/);
});

test('Insert writes plain markdown at the caret and is never offered read-only (§3b, RS-03)', () => {
    // An image is commonmark's inline image node (`![name](path)`), never
    // an image block; a link is the file's name, linked, with the link mark
    // not carried on into typing. (The round trip keeping `![name](path)`
    // byte for byte is pinned with the embeds, §3c.)
    assert.match(SOURCE, /imageSchema\.type\(ctx\)\.create\(\{ src: href, alt: label \}\)/);
    assert.match(SOURCE, /state\.schema\.text\(label, \[link\.create\(\{ href \}\)\]\)/);
    assert.match(SOURCE, /\.removeStoredMark\(link\)/);
    assert.doesNotMatch(SOURCE, /imageBlockSchema\.type\(/);
    // One writer, insertAt, for the list and a drop.
    assert.equal((SOURCE.match(/const \{ kind, href, label \} = insertionFor\(filename, cellPath\);/g) || []).length, 1);
    assert.match(SOURCE, /const insertAt = \(view, ctx, cellPath\) => \{/);
    assert.match(SOURCE, /if \(cellPath\) insertAt\(view, ctx, cellPath\);/);
    assert.match(SOURCE, /if \(!crepe \|\| isReadOnly\) return;/);
    // The list is the cell's files from the universal getFilePaths.
    assert.match(SOURCE, /api\.getFilePaths\(\)\.then\(/);
    assert.match(SOURCE, /setPaths\(insertCandidates\(all, filename\)\)/);
    // No top-right Insert button: the slash item opens the one list, which
    // exists only while open and the editor is editable.
    assert.ok(!SOURCE.includes('Insert' + 'Control'), 'no top-right Insert button');
    assert.doesNotMatch(SOURCE, /title="Insert a file of this cell"/);
    assert.match(SOURCE, /\{insertOpen && !isReadOnly && \(\s*<InsertList\b/);
    assert.equal((SOURCE.match(/<InsertList\b/g) || []).length, 1);
    assert.equal((SOURCE.match(/setInsertOpen\(true\)/g) || []).length, 1, 'only the slash item opens it');
});

// ---------------------------------------------------------------------------
// Drag from the Files pane (RS-03)
// ---------------------------------------------------------------------------

/** A dataTransfer as the Files pane's drag carries it: the cell-files type
 *  with its JSON payload. */
function cellFilesTransfer(payload: unknown, extra: Record<string, string> = {}) {
    const data: Record<string, string> = {
        'application/x-collimate-files': typeof payload === 'string' ? payload : JSON.stringify(payload),
        ...extra,
    };
    return { types: Object.keys(data), getData: (t: string) => data[t] ?? '', dropEffect: 'none' };
}

test('a drag of this cell’s files drops at the pointer; another cell’s, or a read-only editor, is refused (RS-03)', () => {
    const api = { getCellId: () => 'cell-a' };
    const placed: Array<[number, string[]]> = [];
    const place = (pos: number, paths: string[]) => placed.push([pos, paths]);
    const view = (editable = true, at: { pos: number } | null = { pos: 42 }) => ({
        editable,
        asked: [] as any[],
        posAtCoords(coords: any) { this.asked.push(coords); return at; },
    });
    const drop = (dataTransfer: any) => ({ dataTransfer, clientX: 120, clientY: 340 });
    // This cell's files: placed at the document position under the pointer.
    const v = view();
    assert.equal(m.dropCellFiles(v, drop(cellFilesTransfer({ cellId: 'cell-a', paths: ['charts/plot.png', 'results.csv'] })), api, place), true);
    assert.deepEqual(v.asked, [{ left: 120, top: 340 }]);
    assert.deepEqual(placed, [[42, ['charts/plot.png', 'results.csv']]]);
    // Another cell's files: consumed (never parsed as text) and refused.
    placed.length = 0;
    assert.equal(m.dropCellFiles(view(), drop(cellFilesTransfer({ cellId: 'cell-b', paths: ['plot.png'] })), api, place), true);
    assert.equal(m.dropCellFiles(view(), drop(cellFilesTransfer('1')), api, place), true);
    assert.equal(m.dropCellFiles(view(), drop(cellFilesTransfer('{not json')), api, place), true);
    // A read-only editor, or a drop outside the document's positions.
    assert.equal(m.dropCellFiles(view(false), drop(cellFilesTransfer({ cellId: 'cell-a', paths: ['a.png'] })), api, place), true);
    assert.equal(m.dropCellFiles(view(true, null), drop(cellFilesTransfer({ cellId: 'cell-a', paths: ['a.png'] })), api, place), true);
    assert.deepEqual(placed, []);
    // Anything else (OS files, text, an internal move) is ProseMirror's.
    const text = { types: ['text/plain'], getData: () => 'x', dropEffect: 'none' };
    assert.equal(m.dropCellFiles(view(), drop(text), api, place), false);
    assert.equal(m.dropCellFiles(view(), { clientX: 0, clientY: 0 }, api, place), false);
    assert.deepEqual(placed, []);
});

test('the editor accepts a cell-file drag as a copy, only while editable (RS-03)', () => {
    let prevented = 0;
    const over = (dataTransfer: any) => ({ dataTransfer, preventDefault() { prevented += 1; } });
    const dt = cellFilesTransfer({ cellId: 'c', paths: ['a.png'] });
    assert.equal(m.allowCellFileDrag({ editable: true }, over(dt)), true);
    assert.equal(dt.dropEffect, 'copy');
    assert.equal(prevented, 1);
    assert.equal(m.allowCellFileDrag({ editable: false }, over(cellFilesTransfer({ cellId: 'c', paths: [] }))), false);
    assert.equal(m.allowCellFileDrag({ editable: true }, over({ types: ['Files'], getData: () => '' })), false);
    assert.equal(prevented, 1);
    // Wired as the view's own props, after the no-uploads overrides, so they
    // run before the upload plugin's handleDrop; the drop writes through
    // insertAt at the selection it moved to the drop point.
    const override = SOURCE.indexOf('transformPastedHTML: stripPastedImages }));');
    const wiring = SOURCE.indexOf('handleDOMEvents: { ...(prev.handleDOMEvents || {}), dragover: allowCellFileDrag },');
    assert.ok(override > 0 && wiring > override, 'the drop wiring follows the no-uploads overrides');
    assert.match(SOURCE, /handleDrop: \(view, event\) => dropCellFiles\(view, event, api,\s*\(pos, paths\) => dropAt\(view, ctx, pos, paths\)\),/);
    assert.match(SOURCE, /view\.dispatch\(state\.tr\.setSelection\(TextSelection\.near\(state\.doc\.resolve\(pos\)\)\)\);/);
    assert.match(SOURCE, /paths\.filter\(\(p\) => p && p !== filename\)\.forEach\(\(cellPath, i\) => \{/);
    assert.match(SOURCE, /insertAt\(view, ctx, cellPath\);\s*\}\);\s*view\.focus\(\);/);
    // The drop helpers are the SDK's (one MIME, one payload reader).
    assert.match(SOURCE, /\bisCellFileDrag,\n\s*readCellFileDrop,/);
});

// ---------------------------------------------------------------------------
// The style: themes, font, text size (RS-03)
// ---------------------------------------------------------------------------

test('the text size is the style’s factor, 100% by default, inside 70%–220%, never localStorage (RS-03)', () => {
    assert.deepEqual({ ...m.TEXT_SIZE }, { min: 0.7, max: 2.2, step: 0.1, default: 1 });
    assert.equal(m.textSizeZoom(null), 1);
    assert.equal(m.textSizeZoom(undefined), 1);
    assert.equal(m.textSizeZoom('1.4'), 1);
    assert.equal(m.textSizeZoom(Number.NaN), 1);
    assert.equal(m.textSizeZoom(1), 1);
    assert.equal(m.textSizeZoom(1.456), 1.46);
    assert.equal(m.textSizeZoom(0.2), 0.7);
    assert.equal(m.textSizeZoom(3), 2.2);
    assert.match(SOURCE, /setProperty\('--colmd-zoom', String\(textSizeZoom\(style\.textSize\)\)\)/);
    assert.match(SOURCE, /size=\{TEXT_SIZE\}/);
    // The style lives with the host, per channel: nothing in the frame's storage.
    assert.doesNotMatch(SOURCE, /localStorage|sessionStorage|readerZoom|ZOOM_KEY/);
    assert.ok(!SOURCE.includes('Reader' + 'SizeControl'));
});

test('the library themes are Crepe’s families on the skin’s scheme, lifted onto :root (RS-03)', () => {
    assert.deepEqual(m.MILKDOWN_THEMES.map((t) => t.id), ['skin', 'classic', 'frame', 'nord']);
    assert.equal(m.MILKDOWN_THEMES[0].label, 'Collimate');
    const root = 'https://cdn.jsdelivr.net/npm/@milkdown/crepe@7.22.3/lib/theme';
    assert.equal(m.libraryThemeUrl('classic', 'light'), `${root}/crepe/style.css`);
    assert.equal(m.libraryThemeUrl('classic', 'dark'), `${root}/crepe-dark/style.css`);
    assert.equal(m.libraryThemeUrl('frame', 'light'), `${root}/frame/style.css`);
    assert.equal(m.libraryThemeUrl('frame', 'dark'), `${root}/frame-dark/style.css`);
    assert.equal(m.libraryThemeUrl('nord', 'light'), `${root}/nord/style.css`);
    assert.equal(m.libraryThemeUrl('nord', 'dark'), `${root}/nord-dark/style.css`);
    for (const id of [null, 'skin', '', 'crepe', 'constructor', 'toString', 42]) {
        assert.equal(m.libraryThemeUrl(id, 'dark'), null, String(id));
    }
    // The root is the common sheets' pin, so the two never drift apart.
    assert.match(SOURCE, /const CREPE_THEME_ROOT = CREPE_THEME\.slice\(0, CREPE_THEME\.lastIndexOf\('\/'\)\);/);
    // The sheet as Crepe ships it: one .milkdown block, its faces dropped.
    const sheet = [
        '.milkdown {',
        '  --crepe-color-background: #1b1c1d;',
        '  --crepe-color-on-background: #f8f9ff;',
        '',
        '  --crepe-font-title: Rubik, Cambria, \'Times New Roman\', Times, serif;',
        '  --crepe-font-code:',
        '    \'JetBrains Mono\', Menlo, Monaco, \'Courier New\', Courier, monospace;',
        '',
        '  --crepe-shadow-1:',
        '    0px 1px 2px 0px rgba(255, 255, 255, 0.3);',
        '}',
    ].join('\n');
    const lifted = m.liftLibraryTheme(sheet);
    assert.ok(lifted.startsWith(':root:root {'), lifted);
    assert.doesNotMatch(lifted, /\.milkdown|--crepe-font-|Rubik|JetBrains/);
    assert.match(lifted, /--crepe-color-background: #1b1c1d;/);
    assert.match(lifted, /--crepe-shadow-1:\s+0px 1px 2px 0px rgba\(255, 255, 255, 0\.3\);/);
    assert.equal(m.liftLibraryTheme('.milkdown{--crepe-color-primary: red;}'), ':root:root {--crepe-color-primary: red;}');
    // One replaceable <style>, cleared for the skin; the theme follows the
    // host's record once it is known.
    assert.equal(m.LIBRARY_THEME_STYLE_ID, 'colmd-library-theme');
    assert.match(SOURCE, /const href = known \? libraryThemeUrl\(style\.theme, scheme\) : null;/);
    assert.match(SOURCE, /themes=\{MILKDOWN_THEMES\}/);
    // The token map is untouched by any of it.
    assert.doesNotMatch(m.CREPE_TOKEN_CSS, /:root:root|colmd/);
});

test('the prose and the chrome read the Crepe variables a library theme redeclares (RS-03)', () => {
    const css = m.milkdownCss();
    const prose = (SOURCE.match(/const PROSE_CSS = `([\s\S]*?)`;/) || [])[1] || '';
    const chrome = (SOURCE.match(/const CHROME_CSS = `([\s\S]*?)`;/) || [])[1] || '';
    assert.ok(prose && chrome);
    // Outside the shared code-block sheet, no ground, ink, surface or accent
    // is read from the skin directly: the --crepe-* variables carry them.
    const own = prose.replace('${CODE_BLOCK_CSS}', '');
    for (const token of ['--sheet', '--sheet-2', '--sheet-3', '--prose-ink', '--ink', '--brass', '--brass-soft', '--shadow']) {
        const reads = (src: string) => (src.match(new RegExp(`var\\(${token}\\)`, 'g')) || []).length;
        assert.equal(reads(chrome), 0, `CHROME_CSS reads ${token}`);
        assert.equal(reads(own), 0, `PROSE_CSS reads ${token}`);
    }
    // The pre fallback reads the Crepe code area too.
    expectDecls(css, '.milkdown .ProseMirror pre', {
        background: 'var(--crepe-color-inline-area)', color: 'var(--crepe-color-on-surface-variant)',
    });
    expectDecls(css, 'body', { background: 'var(--crepe-color-background)' });
});

test('a fenced code block takes a library theme’s code area and ink, over the shared skin sheet (RS-03)', () => {
    const css = m.milkdownCss();
    // The shared sheet is untouched and first; the
    // library-theme layer comes after it, one :root above each selector.
    assert.ok(css.indexOf(m.CODE_BLOCK_CSS) >= 0);
    assert.ok(css.indexOf(m.CODE_BLOCK_CREPE_CSS) > css.indexOf(m.CODE_BLOCK_CSS));
    const layer = parse(m.CODE_BLOCK_CREPE_CSS);
    assert.ok(layer.length > 0);
    const shared = new Set(parse(m.CODE_BLOCK_CSS).flatMap((r) => r.selectors));
    for (const r of layer) {
        for (const sel of r.selectors) {
            assert.ok(sel.startsWith(':root '), `${sel} sits one :root above the shared rule`);
            assert.ok(shared.has(sel.slice(':root '.length)), `${sel} restates a shared rule`);
        }
        // Every value is a Crepe variable: no skin token read directly.
        for (const [prop, value] of r.decls) {
            assert.match(value, /^var\(--crepe-[a-z-]+\)( !important)?$/, `${r.selectors[0]} { ${prop}: ${value} }`);
        }
    }
    const base = ':root .milkdown .milkdown-code-block ';
    expectDecls(css, ':root .milkdown .ProseMirror .milkdown-code-block', {
        background: 'var(--crepe-color-inline-area)', color: 'var(--crepe-color-on-surface-variant)',
    });
    expectDecls(css, base + '.cm-content', { color: 'var(--crepe-color-on-surface-variant)' });
    expectDecls(css, base + '.cm-gutters', { color: 'var(--crepe-color-outline)' });
    expectDecls(css, base + '.cm-panels', { background: 'var(--crepe-color-surface)', color: 'var(--crepe-color-on-surface)' });
    expectDecls(css, base + '.cm-tooltip', { background: 'var(--crepe-color-surface)', color: 'var(--crepe-color-on-surface)' });
    expectDecls(css, base + '.cm-textfield', { background: 'var(--crepe-color-surface-low)', color: 'var(--crepe-color-on-surface)' });
    expectDecls(css, base + '.cm-foldPlaceholder', { background: 'var(--crepe-color-surface-low)', color: 'var(--crepe-color-outline)' });
    // Under the skin each of those variables is the token the shared sheet names.
    const tokens = new Map<string, string>();
    for (const r of parse(m.CREPE_TOKEN_CSS)) for (const [k, v] of r.decls) tokens.set(k, v);
    assert.equal(tokens.get('--crepe-color-inline-area'), 'var(--code-bg)');
    assert.equal(tokens.get('--crepe-color-on-surface-variant'), 'var(--ink-2)');
    assert.equal(tokens.get('--crepe-color-outline'), 'var(--ink-3)');
    assert.equal(tokens.get('--crepe-color-surface'), 'var(--sheet-2)');
    assert.equal(tokens.get('--crepe-color-surface-low'), 'var(--sheet-3)');
    assert.equal(tokens.get('--crepe-color-on-surface'), 'var(--ink)');
    assert.equal(tokens.get('--crepe-color-selected'), 'var(--brass-soft)');
});

test('the prose face is the style’s family, else the skin’s (RS-03)', () => {
    const css = m.milkdownCss();
    for (const sel of ['.milkdown > .ProseMirror', '.milkdown .ProseMirror h1', '.milkdown .ProseMirror h6']) {
        expectDecls(css, sel, { 'font-family': 'var(--colmd-font, var(--font-prose))' });
    }
    const families = [
        { family: 'Lora', label: 'Lora', role: 'prose', stack: '"Lora", serif-stack' },
        { family: 'Inter', label: 'Inter', role: 'ui', stack: '"Inter", sans-stack' },
    ];
    assert.equal(m.fontStack('Lora', families), '"Lora", serif-stack');
    assert.equal(m.fontStack('Inter', families), '"Inter", sans-stack');
    assert.equal(m.fontStack(null, families), null);
    assert.equal(m.fontStack('Gone', families), null);
    assert.equal(m.fontStack('Lora', []), null);
    assert.match(SOURCE, /if \(stack\) root\.style\.setProperty\('--colmd-font', stack\);\s*else root\.style\.removeProperty\('--colmd-font'\);/);
    assert.match(SOURCE, /font="prose"/);
    // The control owns its reach (shown only near its corner; the SDK's
    // tests/test_style_control_reach.py): it watches its parent, the editor
    // root, and the editor keeps no hover state of its own.
    assert.match(SOURCE, /<div ref=\{containerRef\} style=\{EDITOR_ROOT\}>/);
    assert.match(SOURCE, /<StyleControl\s+api=\{api\}\s+themes=\{MILKDOWN_THEMES\}\s+font="prose"\s+size=\{TEXT_SIZE\}\s+style=\{\{ top: '0\.5rem', right: '0\.875rem', zIndex: 20 \}\}\s*\/>/);
    assert.doesNotMatch(SOURCE, /visible=\{|setHovered|onMouseEnter=\{\(\) => setHover/);
});

// ---------------------------------------------------------------------------
// Caret at the end (RS-03)
// ---------------------------------------------------------------------------

test('a press below the last block puts the caret at the end of the document (RS-03)', () => {
    // The last block that takes up space: folded blocks and widgets do not.
    const el = (bottom: number, height = 20, prev: any = null) => ({
        getBoundingClientRect: () => ({ bottom, height, width: height ? 600 : 0 }),
        previousElementSibling: prev,
    });
    const first = el(100);
    const folded = el(0, 0, first);
    const pm = { lastElementChild: folded };
    assert.equal(m.belowLastBlock(pm, 120), true);
    assert.equal(m.belowLastBlock(pm, 90), false);
    assert.equal(m.belowLastBlock({ lastElementChild: el(300, 20, null) }, 290), false);
    assert.equal(m.belowLastBlock({ lastElementChild: null }, 500), false);
    // The press: captured on the editor column, only on the column, .milkdown
    // or .ProseMirror (never a widget hanging below the text), only while
    // editable; the caret goes to the end, after an empty paragraph appended
    // when the last block is not a textblock, and the editor takes focus.
    assert.match(SOURCE, /colEl\.addEventListener\('mousedown', onColumnMouseDown, true\)/);
    assert.match(SOURCE, /const ground = target === colEl \|\| target === pm\s*\|\| \(target && target\.classList && target\.classList\.contains\('milkdown'\)\);/);
    assert.match(SOURCE, /if \(!pm \|\| !ground \|\| !belowLastBlock\(pm, e\.clientY\)\) return;/);
    assert.match(SOURCE, /if \(!view\.editable\) return;\s*e\.preventDefault\(\);\s*e\.stopPropagation\(\);\s*placeCaretAtEnd\(view\);/);
    assert.match(SOURCE, /if \(\(!last \|\| !last\.isTextblock\) && paragraph\) tr = tr\.insert\(state\.doc\.content\.size, paragraph\.create\(\)\);/);
    assert.match(SOURCE, /view\.dispatch\(tr\.setSelection\(TextSelection\.atEnd\(tr\.doc\)\)\.scrollIntoView\(\)\);\s*view\.focus\(\);/);
});

// ---------------------------------------------------------------------------
// External content never reverts what the user sees. Crepe loads from esm.sh
// at runtime, so these pin the source: the replacement path and its guards.
// ---------------------------------------------------------------------------

test('an external body replaces only the changed range, out of undo history, never a state rebuild', () => {
    // No full-state rebuild: replaceAll(…, true) reset the selection to the
    // document start and dropped undo history on every external write.
    assert.doesNotMatch(SOURCE, /replaceAll\(/);
    assert.match(SOURCE, /crepe\.editor\.action\(replaceChangedRange\(fresh\)\);/);
    // One transaction over the documents' differing span: the selection maps
    // through it, and plugin-listener skips addToHistory:false transactions.
    assert.match(SOURCE, /state\.doc\.content\.findDiffStart\(next\.content\)/);
    assert.match(SOURCE, /state\.doc\.content\.findDiffEnd\(next\.content\)/);
    assert.match(SOURCE, /state\.tr\.replace\(start, endA, next\.slice\(start, endB\)\)/);
    assert.match(SOURCE, /tr\.setMeta\('addToHistory', false\);\s*view\.dispatch\(tr\);/);
    // The scroll column keeps its offset across the replacement.
    assert.match(SOURCE, /const scrollTop = editorColRef\.current\?\.scrollTop;[\s\S]*?editorColRef\.current\.scrollTop = scrollTop;/);
});

test('only host-selected content can replace the primary document', () => {
    assert.doesNotMatch(SOURCE, /api\.getSiblingFile\(/);
    assert.match(SOURCE, /api\.onDocumentSnapshot\(/);
    // A changed file of the cell only re-resolves the embeds of that file
    // (§3c); it never reloads the document.
    assert.equal((SOURCE.match(/api\.onFilesChange\(/g) || []).length, 1);
    assert.match(SOURCE, /api\.onFilesChange\(\(detail\) => embeds\.refresh\(detail && detail\.changedFiles\)\)/);
});

test('every document change publishes a snapshot immediately, excluding host bodies and selection changes', () => {
    const snapshots: string[] = [];
    let external = false;
    const view = m.documentSnapshotView(() => snapshots.push('snapshot'), () => external);
    const doc = (id: string) => ({ id, eq(other: { id: string }) { return this.id === other.id; } });
    const docA = doc('a');
    const docB = doc('b');
    view.update({ state: { doc: docA } }, { doc: docA });
    assert.deepEqual(snapshots, []);
    view.update({ state: { doc: docB } }, { doc: docA });
    view.update({ state: { doc: docA } }, { doc: docB });
    assert.deepEqual(snapshots, ['snapshot', 'snapshot']);
    external = true;
    view.update({ state: { doc: docB } }, { doc: docA });
    assert.deepEqual(snapshots, ['snapshot', 'snapshot']);
    assert.match(SOURCE, /payload: JSON\.stringify\(doc\.toJSON\(\)\)/);
    assert.match(SOURCE, /api\.saveSnapshot\(snapshot\)/);
});

// ---------------------------------------------------------------------------
// Embeds (§3c): the encoding (embed-markdown.ts, shared with the snapshot
// codec), the previews and the toolbar's writes (embeds.ts). The round trips
// run the real pinned Milkdown parser and serializer (sdk/scripts/node_modules)
// with Crepe's image-block component and the editor's embed schema and
// remark transform, registered as main.tsx registers them.
// ---------------------------------------------------------------------------

import * as E from './embed-markdown.ts';
import * as X from './embeds.ts';

type Harness = {
    schema: any;
    parse: (markdown: string) => any;
    serialize: (doc: any) => string;
    remark: any;
    remarkPlugins: Array<{ plugin: any }>;
    crepeImageBlock: () => (tree: any) => void;
    EditorState: any;
    NodeSelection: any;
    TextSelection: any;
};
let md: Harness;

before(async () => {
    // Milkdown's ctx timers signal through the global event target, as in a
    // browser (and in the snapshot codec's worker).
    const events = new EventTarget();
    for (const key of ['addEventListener', 'removeEventListener', 'dispatchEvent'] as const) {
        (globalThis as any)[key] = (events as any)[key].bind(events);
    }
    const lib = path.join(process.cwd(), 'sdk', 'scripts', 'node_modules', '@milkdown', 'kit', 'lib');
    const kit = (name: string) => import(pathToFileURL(path.join(lib, name)).href);
    const { Clock, Container, Ctx } = await kit('ctx.js');
    const core = await kit('core.js');
    const { ParserState, SerializerState } = await kit('transformer.js');
    const commonmark = await kit('preset/commonmark.js');
    const gfm = await kit('preset/gfm.js');
    const imageBlock = await kit('component/image-block.js');
    const state = await kit('prose/state.js');
    const build = async (plugins: any[]) => {
        const ctx = new Ctx(new Container(), new Clock());
        ctx.inject(core.editorViewCtx, { state: { doc: null } });
        const handlers = plugins.map((plugin) => plugin(ctx));
        await Promise.all(handlers.map((handler) => handler()));
        return ctx;
    };
    // main.tsx's embedRemarkPlugin: the transform first among the remark plugins.
    const embedRemark = (ctx: any) => async () => {
        await ctx.wait(core.InitReady);
        ctx.update(core.remarkPluginsCtx, (list: any[]) => [{ plugin: E.remarkEmbedAttributes, options: {} }, ...list]);
    };
    // Crepe's features first, the editor's extensions after (as buildCrepe
    // uses them), so the extended nodes replace Crepe's.
    const ctx = await build([
        core.schema, core.init(null), core.config(() => {}),
        ...commonmark.schema, ...gfm.schema, ...gfm.remarkGFMPlugin, ...commonmark.remarkPreserveEmptyLinePlugin,
        ...imageBlock.imageBlockSchema, ...imageBlock.remarkImageBlockPlugin,
        ...commonmark.imageSchema.extendSchema(E.extendImageSchema),
        ...imageBlock.imageBlockSchema.extendSchema(E.extendImageBlockSchema),
        embedRemark,
    ]);
    const schema = ctx.get(core.schemaCtx);
    const remark = ctx.get(core.remarkCtx);
    const parser = ParserState.create(schema, remark);
    // Crepe's own remark-image-block, alone, for the ordering cases.
    const crepeOnly = await build([core.init(null), core.config(() => {}), ...imageBlock.remarkImageBlockPlugin]);
    md = {
        schema,
        parse: (text) => parser(text),
        serialize: (doc) => {
            ctx.set(core.editorViewCtx, { state: { doc } });
            return SerializerState.create(schema, remark)(doc);
        },
        remark,
        remarkPlugins: ctx.get(core.remarkPluginsCtx),
        crepeImageBlock: crepeOnly.get(core.remarkPluginsCtx)[0].plugin,
        EditorState: state.EditorState,
        NodeSelection: state.NodeSelection,
        TextSelection: state.TextSelection,
    };
});

const roundTrip = (text: string) => md.serialize(md.parse(text));
const firstBlock = (text: string) => md.parse(text).firstChild;

test('the attribute block grammar: width=NN% and align=left|center|right, each at most once (§3c)', () => {
    assert.deepEqual(E.parseEmbedBlock('{width=60% align=center}'), { width: 60, align: 'center', length: 24 });
    assert.deepEqual(E.parseEmbedBlock('{align=right width=5%} tail'), { width: 5, align: 'right', length: 22 });
    assert.deepEqual(E.parseEmbedBlock('{width=100%}'), { width: 100, align: null, length: 12 });
    assert.deepEqual(E.parseEmbedBlock('{ align=left }'), { width: null, align: 'left', length: 14 });
    for (const text of ['{}', '{ }', '{width=0%}', '{width=101%}', '{width=60}', '{width=60px}', '{width=6.5%}',
        '{align=justify}', '{width=60% width=50%}', '{align=left align=right}', '{foo}', '{width=60% foo=1}',
        ' {width=60%}', 'x{width=60%}', '{width=60%', '{width=60%\n}', '{{width=60%}}', null, 3]) {
        assert.equal(E.parseEmbedBlock(text), null, JSON.stringify(text));
    }
    // Written only when set, width first.
    assert.equal(E.formatEmbedBlock({ width: null, align: null }), '');
    assert.equal(E.formatEmbedBlock(null), '');
    assert.equal(E.formatEmbedBlock({ width: 60, align: null }), '{width=60%}');
    assert.equal(E.formatEmbedBlock({ width: null, align: 'center' }), '{align=center}');
    assert.equal(E.formatEmbedBlock({ align: 'right', width: 25 }), '{width=25% align=right}');
    // Out-of-grammar values are never written.
    assert.equal(E.formatEmbedBlock({ width: 0, align: 'middle' }), '');
    assert.equal(E.formatEmbedBlock({ width: 33.3, align: null }), '');
});

test('every embed form round-trips byte for byte through the editor (§3c)', () => {
    const forms = [
        // Block (an image alone in its paragraph): none, width, align, both,
        // with a caption, and an empty alt.
        '![plot](charts/plot.png)\n',
        '![plot](charts/plot.png){width=60%}\n',
        '![plot](charts/plot.png){align=right}\n',
        '![plot](charts/plot.png){width=60% align=center}\n',
        '![plot](charts/plot.png "Weekly totals"){width=75% align=left}\n',
        '![](charts/plot.png){width=50%}\n',
        // Inline: none, width, align, both.
        'see ![plot](charts/plot.png) here\n',
        'see ![plot](charts/plot.png){width=25%} here\n',
        'see ![plot](charts/plot.png){align=right} here\n',
        'see ![plot](charts/plot.png "t"){width=10% align=center}, then text\n',
        // The link form, and a link and a preview of the same file.
        '[plot](charts/plot.png)\n',
        '[plot](charts/plot.png) and ![plot](charts/plot.png){width=40%}\n',
        // Media and file cards are the same markdown.
        '![clip](media/clip.mp4){width=100% align=center}\n',
        '![talk](audio/talk.mp3)\n',
        '![data](data.csv)\n',
        'inline ![report](report.pdf){width=30%} card\n',
        // Web images keep their URL.
        '![logo](https://example.com/logo.png){width=20%}\n',
    ];
    for (const form of forms) assert.equal(roundTrip(form), form, form);
    // Braces that are not an attribute block stay text, escaped as before;
    // a block after a space is text too.
    assert.equal(roundTrip('![plot](charts/plot.png){foo}\n'), '![plot](charts/plot.png){foo}\n');
    assert.equal(roundTrip('![plot](charts/plot.png) {width=60%}\n'), '![plot](charts/plot.png) {width=60%}\n');
});

test('the alt is the alt: no height ratio is ever written into it (§3c)', () => {
    // Crepe's image block wrote `![1.00](…)`; the extended block keeps the alt.
    const block = firstBlock('![plot](charts/plot.png "cap"){width=60% align=center}\n');
    assert.equal(block.type.name, 'image-block');
    assert.deepEqual({ ...block.attrs }, { src: 'charts/plot.png', caption: 'cap', alt: 'plot', width: 60, align: 'center' });
    assert.equal('ratio' in block.attrs, false);
    const inline = md.parse('a ![plot](p.png){align=left} b').firstChild.child(1);
    // An omitted title is the empty string: the attribute is a validated string.
    assert.deepEqual({ ...inline.attrs }, { src: 'p.png', alt: 'plot', title: '', width: null, align: 'left' });
    // A ratio-shaped alt an older editor wrote is just an alt now.
    assert.equal(roundTrip('![1.00](chart.png)\n'), '![1.00](chart.png)\n');
});

test('Insert’s `![name](path)` survives the editor unchanged, as an inline image and once it is a block (§3c)', () => {
    const { schema } = md;
    // What insertAt writes: commonmark's image node, alt = the file's name.
    const image = schema.nodes.image.create({ src: 'charts/plot.png', alt: 'plot.png' });
    const written = md.serialize(schema.nodes.doc.create(null, schema.nodes.paragraph.create(null, image)));
    assert.equal(written, '![plot.png](charts/plot.png)\n');
    // Reloaded it is an image block, and it writes back the same bytes.
    assert.equal(firstBlock(written).type.name, 'image-block');
    assert.equal(roundTrip(written), written);
    assert.equal(roundTrip(roundTrip(written)), written);
});

test('the remark transform reads the block and makes the image block itself, before or after Crepe’s (§3c)', () => {
    // main.tsx puts it first among the remark plugins.
    assert.equal(md.remarkPlugins[0].plugin, E.remarkEmbedAttributes);
    assert.match(SOURCE, /ctx\.update\(remarkPluginsCtx, \(list\) => \[plugin, \.\.\.list\]\);/);
    const strip = (tree: any) => JSON.parse(JSON.stringify(tree, (k, v) => (k === 'position' ? undefined : v)));
    const ours = () => E.remarkEmbedAttributes();
    const crepe = () => md.crepeImageBlock();
    const run = (text: string, order: Array<() => (tree: any) => void>) => {
        const tree = md.remark.parse(text);
        for (const transform of order) transform()(tree);
        return strip(tree);
    };
    for (const text of ['![plot](a.png){width=60% align=center}\n', '![plot](a.png "c")\n', 'x ![plot](a.png){width=5%} y\n']) {
        const first = run(text, [ours, crepe]);
        assert.deepEqual(run(text, [crepe, ours]), first, `${text}: the order does not matter`);
        assert.deepEqual(run(text, [ours]), first, `${text}: Crepe's finds nothing left to do`);
    }
    // The block's layout rides on the image-block node; the text is gone.
    assert.deepEqual(run('![plot](a.png){width=60% align=center}\n', [ours]).children, [
        { type: 'image-block', url: 'a.png', alt: 'plot', title: null, data: { colmdEmbed: { width: 60, align: 'center' } } },
    ]);
    // Crepe's alone would have kept the braces as text, so a sized image
    // alone on its line stayed inline, and dropped the alt into a ratio.
    const crepeAlone = run('![plot](a.png){width=60%}\n', [crepe]).children[0];
    assert.equal(crepeAlone.type, 'paragraph');
    // Inside a paragraph, only the leading block of the following text goes.
    const inline = run('a ![p](x.png){align=right} rest {width=9%}\n', [ours]).children[0].children;
    assert.deepEqual(inline.map((n: any) => n.type), ['text', 'image', 'text']);
    assert.equal(inline[2].value, ' rest {width=9%}');
});

test('the snapshot codec carries the same embed schema as the editor (§3c)', () => {
    const codec = fs.readFileSync(path.join(process.cwd(), 'sdk', 'scripts', 'snapshot-codecs', 'milkdown.ts'), 'utf8');
    assert.match(codec, /from '\.\/embed-markdown\.ts'/);
    assert.match(codec, /imageSchema\.extendSchema\(extendImageSchema\)/);
    assert.match(codec, /imageBlockSchema\.extendSchema\(extendImageBlockSchema\)/);
    assert.match(SOURCE, /\.use\(imageSchema\.extendSchema\(extendImageSchema\)\)\s*\.use\(imageBlockSchema\.extendSchema\(extendImageBlockSchema\)\)\s*\.use\(embedRemarkPlugin\);/);
});

test('an embed previews by its target: image, video, audio, a file card or a missing-file card (§3c)', () => {
    const served = new Set(['notes/plot.png', 'notes/clip.webm', 'notes/talk.mp3', 'notes/data.csv', 'top.png']);
    const fileUrl = (p: string) => (served.has(p) ? `/files/${p}?revision=1` : null);
    const kind = (src: string) => X.resolveEmbed(src, 'notes/doc.md', fileUrl);
    assert.deepEqual(kind('plot.png'), { kind: 'image', cellPath: 'notes/plot.png', url: '/files/notes/plot.png?revision=1', name: 'plot.png' });
    assert.equal(kind('clip.webm').kind, 'video');
    assert.equal(kind('talk.mp3').kind, 'audio');
    assert.deepEqual(kind('data.csv'), { kind: 'file', cellPath: 'notes/data.csv', url: '/files/notes/data.csv?revision=1', name: 'data.csv' });
    assert.equal(kind('../top.png').kind, 'image');
    // A path the host refuses is a missing-file card, whatever its type.
    assert.deepEqual(kind('gone.png'), { kind: 'missing', cellPath: 'notes/gone.png', url: null, name: 'gone.png' });
    assert.equal(kind('gone.mp4').kind, 'missing');
    // External targets: an image unless they name a video or an audio file.
    assert.deepEqual(kind('https://example.com/x'), { kind: 'image', cellPath: null, url: 'https://example.com/x', name: 'x' });
    assert.equal(kind('https://example.com/a.mp4').kind, 'video');
    // An empty src is Crepe's link field.
    assert.equal(kind('').kind, 'image');
    // Extensions, case-blind, ignoring a query or a fragment.
    assert.equal(X.previewKind('A.JPG'), 'image');
    assert.equal(X.previewKind('a.mov?x=1'), 'video');
    assert.equal(X.previewKind('a.flac#t'), 'audio');
    assert.equal(X.previewKind('a.pdf'), null);
    assert.equal(X.baseName('dir/my%20plot.png'), 'my plot.png');
    // Only images, video and audio take a size and an alignment.
    assert.deepEqual(['image', 'video', 'audio', 'file', 'missing'].map(X.embedSizable), [true, true, true, false, false]);
});

test('a path the host refuses never throws: Crepe’s views load nothing and the embed shows a missing card (§3c)', () => {
    const fileUrl = (p: string) => (p === 'plot.png' ? '/files/plot.png' : null);
    assert.equal(X.proxiedEmbedUrl('plot.png', 'doc.md', fileUrl), '/files/plot.png');
    assert.equal(X.proxiedEmbedUrl('gone.png', 'doc.md', fileUrl), '');
    assert.equal(X.proxiedEmbedUrl('https://example.com/x.png', 'doc.md', fileUrl), 'https://example.com/x.png');
    // The editor's fileUrl turns the host's refusal (getFileUrl throws for a
    // path it does not serve) into null, and Crepe's proxyDomURL goes
    // through it.
    assert.match(SOURCE, /const fileUrl = \(cellPath\) => \{\s*try \{ return api\.getFileUrl\(cellPath\); \} catch \{ return null; \}\s*\};/);
    assert.match(SOURCE, /proxyDomURL: \(url\) => proxiedEmbedUrl\(url, filename, fileUrl\),/);
    assert.equal((SOURCE.match(/api\.getFileUrl\(/g) || []).length, 1, 'every file URL goes through fileUrl');
});

test('an embed lays out by its width and alignment, block and inline (§3c)', () => {
    const block = (layout: any) => X.embedLayoutStyles(layout, false);
    assert.equal(block({ width: null, align: null }).wrapper.alignItems, 'flex-start');
    assert.equal(block({ width: null, align: null }).box.width, 'auto');
    assert.equal(block({ width: 60, align: 'center' }).wrapper.alignItems, 'center');
    assert.equal(block({ width: 60, align: 'center' }).box.width, '60%');
    assert.equal(block({ width: 25, align: 'right' }).wrapper.alignItems, 'flex-end');
    assert.equal(X.embedLayoutStyles({ width: 25, align: 'right' }, false, true).box.width, 'auto', 'an empty image fills');
    const inline = (layout: any) => X.embedLayoutStyles(layout, true).wrapper;
    assert.deepEqual([inline({}).display, inline({}).float, inline({}).width], ['inline-block', 'none', 'auto']);
    assert.deepEqual([inline({ width: 30, align: 'left' }).float, inline({ width: 30, align: 'left' }).width], ['left', '30%']);
    assert.equal(inline({ align: 'right' }).float, 'right');
    assert.deepEqual([inline({ align: 'center' }).display, inline({ align: 'center' }).margin], ['block', '6px auto']);
    // Values outside the encoding are ignored.
    assert.equal(block({ width: 500, align: 'middle' }).box.width, 'auto');
    // Crepe's height-ratio handle is hidden and an image keeps its aspect.
    const css = m.milkdownCss();
    expectDecls(css, '.milkdown .milkdown-image-block > .image-wrapper > .image-resize-handle', { display: 'none !important' });
    expectDecls(css, '.milkdown .milkdown-image-block > .image-wrapper img', { height: 'auto !important', 'min-height': '0' });
    expectDecls(css, '.milkdown .colmd-embed[data-embed-width] .milkdown-image-block > .image-wrapper img', { width: '100%' });
});

test('the size handle writes a whole percent from the edge that moves (§3c)', () => {
    const column = { left: 100, right: 500 };
    // Left (or unaligned): from the box's left edge.
    assert.equal(X.draggedWidth(null, { left: 100, right: 300 }, column, 340), 60);
    assert.equal(X.draggedWidth('left', { left: 100, right: 300 }, column, 340), 60);
    // Right: from the right edge, leftwards.
    assert.equal(X.draggedWidth('right', { left: 300, right: 500 }, column, 400), 25);
    // Centre: twice the distance from the centre.
    assert.equal(X.draggedWidth('center', { left: 200, right: 400 }, column, 400), 50);
    // Clamped to EMBED_MIN_WIDTH..100.
    assert.equal(X.draggedWidth(null, { left: 100, right: 300 }, column, 90), E.EMBED_MIN_WIDTH);
    assert.equal(X.draggedWidth(null, { left: 100, right: 300 }, column, 900), 100);
    assert.equal(X.draggedWidth(null, { left: 0, right: 0 }, { left: 5, right: 5 }, 10), null);
});

test('the toolbar never covers the lines around its target: on an image or video’s top edge, else below (§3c)', () => {
    const column = { left: 0, right: 400, top: 0, bottom: 600 };
    const size = { width: 200, height: 30 };
    const I = X.EMBED_TOOLBAR_INSET;
    const half = X.EMBED_HANDLE_WIDTH / 2;
    const box = { left: 50, right: 350, top: 100, bottom: 300 };
    const host = (anchor: any, align: any = null) => X.placeEmbedToolbar(anchor, column, size, { host: true, align });
    // An image or a video hosts it, inside its box along the top edge, at
    // the edge away from the size handle.
    assert.deepEqual([X.embedHostsToolbar('image'), X.embedHostsToolbar('video')], [true, true]);
    assert.deepEqual(['audio', 'file', 'missing'].map(X.embedHostsToolbar), [false, false, false]);
    assert.deepEqual(host(box), { left: 50 + I, top: 100 + I, side: 'top' });
    assert.deepEqual(host(box, 'center'), { left: 50 + I, top: 100 + I, side: 'top' });
    assert.deepEqual(host(box, 'right'), { left: 350 - I - 200, top: 100 + I, side: 'top' });
    // At the very top of the column it stays inside, never above the box.
    assert.equal(host({ ...box, top: 0 }).top, I);
    // A tall box scrolled under the column's top keeps it in view, and
    // takes it along as it leaves, never onto the text below.
    assert.equal(host({ ...box, top: -150, bottom: 150 }).top, X.EMBED_TOOLBAR_MARGIN);
    const leaving = host({ ...box, top: -250, bottom: 50 });
    assert.equal(leaving.top + size.height, 50 - I - X.EMBED_HANDLE_MIN);
    // A short box (an inline image on a line of text) keeps it on its top edge.
    const inline = { left: 50, right: 350, top: 100, bottom: 148 };
    assert.deepEqual(host(inline), { left: 50 + I, top: 100 + I, side: 'top' });
    // Narrower than the toolbar and too short to take the handle below it:
    // past the handle, beside the box, on either side.
    const narrow = { left: 50, right: 130, top: 100, bottom: 148 };
    assert.deepEqual(host(narrow), { left: 130 + half + X.EMBED_TOOLBAR_GAP, top: 100 + I, side: 'top' });
    const narrowRight = { left: 300, right: 380, top: 100, bottom: 148 };
    assert.equal(host(narrowRight, 'right').left, 300 - half - X.EMBED_TOOLBAR_GAP - 200);
    // Narrow but tall: inside, the handle goes below it (placeEmbedHandle).
    assert.equal(host({ ...narrow, bottom: 300 }).left, 50 + I);
    // A link (or an audio player, a card): below its line, flipping above
    // only without room below, and below again with room for neither.
    const line = { left: 60, right: 120, top: 100, bottom: 120 };
    assert.deepEqual(X.placeEmbedToolbar(line, column, size), { left: 60, top: 120 + X.EMBED_TOOLBAR_GAP, side: 'below' });
    assert.deepEqual(X.placeEmbedToolbar({ ...line, top: 560, bottom: 580 }, column, size),
        { left: 60, top: 560 - X.EMBED_TOOLBAR_GAP - 30, side: 'above' });
    assert.equal(X.placeEmbedToolbar({ ...line, top: 20, bottom: 590 }, column, size).side, 'below');
    assert.equal(X.placeEmbedToolbar(box, column, size, { host: false, align: 'right' }).side, 'below');
    // Kept inside the column's margins, on a box or below a line.
    assert.equal(X.placeEmbedToolbar({ left: 350, right: 390, top: 100, bottom: 120 }, column, size).left, 400 - X.EMBED_TOOLBAR_MARGIN - 200);
    assert.equal(X.placeEmbedToolbar({ left: -40, right: 10, top: 100, bottom: 120 }, column, size).left, X.EMBED_TOOLBAR_MARGIN);
    assert.equal(host({ left: 300, right: 400, top: 100, bottom: 300 }).left, 400 - X.EMBED_TOOLBAR_MARGIN - 200);
    // Relative to the column.
    assert.deepEqual(X.placeEmbedToolbar({ left: 150, right: 450, top: 150, bottom: 350 }, { left: 100, right: 500, top: 50, bottom: 650 }, size, { host: true }),
        { left: 50 + I, top: 100 + I, side: 'top' });
    // The editor places it so, with the box the target's.
    assert.match(SOURCE, /placeEmbedToolbar\(anchor, bounds, size, \{ host: embed && embedHostsToolbar\(kind\), align \}\)/);
});

test('the size handle sits on the edge that moves, clear of the toolbar (§3c)', () => {
    const column = { left: 0, right: 400, top: 0, bottom: 600 };
    const half = X.EMBED_HANDLE_WIDTH / 2;
    const box = { left: 50, right: 350, top: 100, bottom: 300 };
    // Centred on the moving edge: the right one, or the left when right-aligned.
    assert.deepEqual(X.placeEmbedHandle(box, column, null, null), { left: 350 - half, top: 182, height: X.EMBED_HANDLE_MAX });
    assert.equal(X.placeEmbedHandle(box, column, 'right', null).left, 50 - half);
    // A toolbar clear of the edge leaves it centred.
    const clear = { left: 58, right: 258, top: 108, bottom: 138 };
    assert.equal(X.placeEmbedHandle(box, column, null, clear).top, 182);
    // A toolbar over the edge (a narrow box) pushes it into the box below.
    const over = { left: 58, right: 358, top: 108, bottom: 138 };
    const pushed = X.placeEmbedHandle(box, column, null, over);
    assert.ok(pushed.top >= over.bottom + X.EMBED_TOOLBAR_GAP, 'below the toolbar');
    assert.ok(pushed.top + pushed.height <= box.bottom, 'inside the box');
    // A short box: as tall as the box allows.
    assert.equal(X.placeEmbedHandle({ left: 0, right: 100, top: 0, bottom: 20 }, column, null, null).height, 20);
    // Relative to the column, at the place's size.
    assert.match(SOURCE, /placeEmbedHandle\(anchor, bounds, align, \{ left, top, right: left \+ size\.width, bottom: top \+ size\.height \}\)/);
    assert.match(SOURCE, /left: handle\.left,\s*top: handle\.top,\s*width: EMBED_HANDLE_WIDTH,\s*height: handle\.height,/);
});

test('a click on a video’s picture selects it without playing; its controls stay native (§3c)', () => {
    const player = (kind: string, bottom: number) => ({
        getAttribute: (name: string) => (name === 'data-embed-media' ? kind : null),
        getBoundingClientRect: () => ({ top: 0, bottom, left: 0, right: 400 }),
    });
    const video = player('video', 240);
    assert.equal(X.pressOnMediaBody(video, 10), true, 'the picture');
    assert.equal(X.pressOnMediaBody(video, 240 - X.MEDIA_CONTROLS_HEIGHT - 1), true);
    assert.equal(X.pressOnMediaBody(video, 240 - X.MEDIA_CONTROLS_HEIGHT), false, 'the controls bar');
    assert.equal(X.pressOnMediaBody(video, 230), false);
    // An audio player is all controls; no player, no rule.
    assert.equal(X.pressOnMediaBody(player('audio', 54), 0), false);
    assert.equal(X.pressOnMediaBody(null, 0), false);
    // The view: the press selects, the click on the picture is kept from
    // toggling playback, only while editable; the controls keep their events.
    const source = fs.readFileSync(path.join(process.cwd(), 'content', 'renderers', 'documents', 'milkdown', 'embeds.ts'), 'utf8');
    assert.match(source, /media\.addEventListener\('pointerdown', selectSelf\);\s*media\.addEventListener\('click', keepPlayback\);/);
    assert.match(source, /if \(view\.editable && pressOnMediaBody\(media, event\.clientY\)\) event\.preventDefault\(\);/);
    assert.match(source, /if \(media && target && media\.contains\(target\)\) return true;/);
    assert.match(source, /el\.controls = true;/);
});

test('every selected embed shows the same brass ring on its box, and only that (§3c)', () => {
    const source = fs.readFileSync(path.join(process.cwd(), 'content', 'renderers', 'documents', 'milkdown', 'embeds.ts'), 'utf8');
    // The view paints it on its box for every kind, an image as a video;
    // an image with no target yet (Crepe's link field) has none.
    assert.match(source, /box\.style\.outline = selected && node\.attrs\.src \? '2px solid var\(--brass\)' : 'none';/);
    // Crepe's own marks inside an embed are off.
    const css = m.milkdownCss();
    expectDecls(css, '.milkdown .colmd-embed .milkdown-image-inline.selected', { outline: 'none' });
    expectDecls(css, '.milkdown .colmd-embed .milkdown-image-block.selected > .image-wrapper::before', { content: 'none' });
});

/** An editor state over `text` with the node at the first embed selected. */
function selectEmbed(text: string) {
    const doc = md.parse(text);
    let pos = -1;
    doc.descendants((node: any, at: number) => {
        if (pos < 0 && (node.type.name === 'image' || node.type.name === 'image-block')) pos = at;
        return pos < 0;
    });
    assert.ok(pos >= 0, `an embed in ${text}`);
    return md.EditorState.create({ doc, selection: md.NodeSelection.create(doc, pos) });
}

/** An editor state over `text` with the caret `offset` characters into its
 *  first occurrence of `needle`. */
function caretIn(text: string, needle: string, offset = 1) {
    const doc = md.parse(text);
    let pos = -1;
    doc.descendants((node: any, at: number) => {
        if (pos < 0 && node.isText && node.text.includes(needle)) pos = at + node.text.indexOf(needle) + offset;
        return pos < 0;
    });
    assert.ok(pos >= 0, `${needle} in ${text}`);
    return md.EditorState.create({ doc, selection: md.TextSelection.create(doc, pos) });
}

test('the toolbar’s target: a selected embed, a link to a file that previews, nothing while read-only (§3c)', () => {
    const state = selectEmbed('![plot](charts/plot.png){width=60%}\n');
    assert.deepEqual(X.embedToolbarTarget(state, true, 'doc.md'), {
        type: 'embed', pos: 0, name: 'image-block',
        attrs: { src: 'charts/plot.png', caption: '', alt: 'plot', width: 60, align: null },
    });
    // An image with no target yet (Crepe's link field) has none.
    const empty = md.schema.nodes.image.create({ src: '' });
    const emptyDoc = md.schema.nodes.doc.create(null, md.schema.nodes.paragraph.create(null, [md.schema.text('a '), empty]));
    const emptyState = md.EditorState.create({ doc: emptyDoc, selection: md.NodeSelection.create(emptyDoc, 3) });
    assert.equal(X.embedToolbarTarget(emptyState, true, 'doc.md'), null);
    // Read-only: no toolbar, for an embed or a link.
    assert.equal(X.embedToolbarTarget(state, false, 'doc.md'), null);
    const link = caretIn('see [plot](charts/plot.png) here\n', 'plot');
    assert.equal(X.embedToolbarTarget(link, false, 'doc.md'), null);
    assert.deepEqual(X.embedToolbarTarget(link, true, 'doc.md'),
        { type: 'link', from: 5, to: 9, href: 'charts/plot.png', title: null, text: 'plot' });
    // The caret on either edge of the link counts.
    assert.equal(X.embedToolbarTarget(caretIn('see [plot](charts/plot.png) here\n', 'plot', 0), true, 'doc.md').type, 'link');
    assert.equal(X.embedToolbarTarget(caretIn('see [plot](charts/plot.png) here\n', 'plot', 4), true, 'doc.md').type, 'link');
    // A link to a file with no preview, a web link, or plain text: none.
    assert.equal(X.embedToolbarTarget(caretIn('[data](data.csv)\n', 'data'), true, 'doc.md'), null);
    assert.equal(X.embedToolbarTarget(caretIn('[web](https://example.com/a.png)\n', 'web'), true, 'doc.md'), null);
    assert.equal(X.embedToolbarTarget(caretIn('plain text\n', 'text'), true, 'doc.md'), null);
    // A video and an audio file preview too.
    assert.equal(X.embedToolbarTarget(caretIn('[clip](clip.mp4)\n', 'clip'), true, 'doc.md').type, 'link');
    assert.equal(X.embedToolbarTarget(caretIn('[talk](talk.ogg)\n', 'talk'), true, 'doc.md').type, 'link');
    // The React side mounts it only for a target and never while read-only.
    assert.match(SOURCE, /\{embedTarget && !isReadOnly && \(\s*<EmbedToolbar\b/);
    assert.match(SOURCE, /const target = view \? embedToolbarTarget\(view\.state, view\.editable, filename\) : null;/);
});

test('the toolbar writes width and alignment into the attribute block, and keeps the embed selected (§3c)', () => {
    const apply = (state: any, patch: any) => state.apply(X.setEmbedLayout(state, state.selection.from, patch));
    let state = selectEmbed('![plot](charts/plot.png)\n');
    state = apply(state, { width: 50 });
    assert.equal(md.serialize(state.doc), '![plot](charts/plot.png){width=50%}\n');
    assert.equal(state.selection.node?.type.name, 'image-block', 'still selected');
    state = apply(state, { align: 'center' });
    assert.equal(md.serialize(state.doc), '![plot](charts/plot.png){width=50% align=center}\n');
    state = apply(state, { width: null });
    assert.equal(md.serialize(state.doc), '![plot](charts/plot.png){align=center}\n');
    state = apply(state, { align: null });
    assert.equal(md.serialize(state.doc), '![plot](charts/plot.png)\n');
    // Inline embeds the same.
    let inline = selectEmbed('a ![p](p.png "t") b\n');
    inline = apply(inline, { width: 25, align: 'right' });
    assert.equal(md.serialize(inline.doc), 'a ![p](p.png "t"){width=25% align=right} b\n');
    // The toolbar: presets 25/50/75/100 (a pressed one clears), Left writes
    // no align, Center and Right toggle; data attributes for the UI tests.
    assert.deepEqual([...X.EMBED_WIDTH_PRESETS], [25, 50, 75, 100]);
    assert.match(SOURCE, /onPress=\{\(\) => setWidth\(attrs\.width === w \? null : w\)\}/);
    assert.match(SOURCE, /onPress=\{\(\) => setAlign\(value === 'left' \|\| align === value \? null : value\)\}/);
    assert.match(SOURCE, /data=\{\{ 'data-embed-width': String\(w\) \}\}/);
    assert.match(SOURCE, /data=\{\{ 'data-embed-align': value \}\}/);
    assert.match(SOURCE, /data-embed-handle="width"/);
    assert.match(SOURCE, /data-embed-toolbar=\{target\.type\}/);
    // A drag previews on the view and writes once, on release.
    assert.match(SOURCE, /applyEmbedLayout\(drag\.dom, \{ width, align \}\);/);
    assert.match(SOURCE, /if \(drag && drag\.width != null && drag\.width !== attrs\.width\) setWidth\(drag\.width\);/);
});

test('Link and Preview turn an embed into its link and back, losing nothing (§3c)', () => {
    // A block preview becomes a paragraph holding the alt, linked; the
    // caret lands on the link, so the toolbar offers Preview.
    let state = selectEmbed('![plot](charts/plot.png "Weekly"){width=60%}\n');
    state = state.apply(X.embedToLink(state, state.selection.from, md.TextSelection));
    assert.equal(md.serialize(state.doc), '[plot](charts/plot.png "Weekly")\n');
    const target = X.embedToolbarTarget(state, true, 'doc.md');
    assert.equal(target?.type, 'link');
    // Preview: the whole-paragraph link is an image block again, selected.
    state = state.apply(X.linkToEmbed(state, target, md.NodeSelection));
    assert.equal(md.serialize(state.doc), '![plot](charts/plot.png "Weekly")\n');
    assert.equal(state.selection.node?.type.name, 'image-block');
    // Inline: the image becomes linked text in place, and back.
    let inline = selectEmbed('a ![p](p.png) b\n');
    inline = inline.apply(X.embedToLink(inline, inline.selection.from, md.TextSelection));
    assert.equal(md.serialize(inline.doc), 'a [p](p.png) b\n');
    const linkTarget = X.embedToolbarTarget(inline, true, 'doc.md');
    inline = inline.apply(X.linkToEmbed(inline, linkTarget, md.NodeSelection));
    assert.equal(md.serialize(inline.doc), 'a ![p](p.png) b\n');
    assert.equal(inline.selection.node?.type.name, 'image');
    // Without an alt the link reads the file's name; a card is linkable too.
    let card = selectEmbed('![](reports/q%203.pdf)\n');
    card = card.apply(X.embedToLink(card, card.selection.from, md.TextSelection));
    assert.equal(md.serialize(card.doc), '[q 3.pdf](reports/q%203.pdf)\n');
    // The link mark is not carried into typing after it.
    assert.equal(card.storedMarks?.length ?? 0, 0);
    // The toolbar's buttons.
    assert.match(SOURCE, /data=\{\{ 'data-embed-action': 'link' \}\}/);
    assert.match(SOURCE, /data=\{\{ 'data-embed-action': 'preview' \}\}/);
    assert.match(SOURCE, /run\(\(state\) => embedToLink\(state, target\.pos, TextSelection\)\)/);
    assert.match(SOURCE, /run\(\(state\) => linkToEmbed\(state, target, NodeSelection\)\)/);
});

test('the embed views and toolbar wiring: Crepe’s views wrapped after create, refresh only for changed files (§3c)', () => {
    assert.match(SOURCE, /view\.setProps\(\{ nodeViews: \{ \.\.\.base, \.\.\.embeds\.nodeViews\(base\) \} \}\);/);
    assert.match(SOURCE, /installEmbedViews\(c\);\s*if \(isReadOnly\) c\.setReadonly\(true\);/);
    assert.match(SOURCE, /c\.editor\.use\(\$prose\(\(\) => createEmbedToolbarPlugin\(reportEmbed\)\)\);/);
    assert.match(SOURCE, /reveal: \(cellPath, options\) => api\.revealFile\(cellPath, options\),/);
    // A file card's Open shows the file in a new tab, never over the document.
    const embedsSource = fs.readFileSync(path.join(process.cwd(), 'content', 'renderers', 'documents', 'milkdown', 'embeds.ts'), 'utf8');
    assert.match(embedsSource, /if \(resolved\.cellPath\) reveal\(resolved\.cellPath, \{ newTab: true \}\);/);
    // Both image nodes get a view; refresh re-resolves only the embeds of
    // the changed files (none live here) and takes a missing list.
    const embeds = X.createEmbeds({ mdPath: 'doc.md', fileUrl: () => null, reveal: () => {}, NodeSelection: md.NodeSelection });
    assert.deepEqual(Object.keys(embeds.nodeViews({})).sort(), ['image', 'image-block']);
    embeds.refresh(['a.png']);
    embeds.refresh(undefined as any);
    const source = fs.readFileSync(path.join(process.cwd(), 'content', 'renderers', 'documents', 'milkdown', 'embeds.ts'), 'utf8');
    assert.match(source, /for \(const view of live\) if \(changed\.has\(view\.cellPath\(\)\)\) view\.refresh\(\);/);
});
