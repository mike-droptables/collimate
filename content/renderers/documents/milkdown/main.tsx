// ---------------------------------------------------------------------------
// Milkdown (Crepe) WYSIWYG markdown renderer.
//
// Section map:
//   §1  Imports + esm.sh dependency pinning (read the DEPS note — load-bearing)
//   §2  Crepe's sheets, the skin CSS (tokens, prose, chrome), the style
//       (library themes, font, text size)
//   §2b Fenced code: the skin's syntax palette on Crepe's CodeMirror
//   §3  Obsidian-style heading folding (decoration plugin)
//   §3b Cell-file links and images: resolving, inserting, dropping, and no
//       uploads
//   §3c Embeds: previews by kind, size and alignment, the embed toolbar
//   §4  UI atoms: InsertList
//   §5  MilkdownEditor: mount lifecycle, save pipeline, cell-file links,
//       caret at the end, wiring
// ---------------------------------------------------------------------------
//
// §1 DEPS — every @milkdown/* import below carries the byte-identical deps
// list:
//
//   @milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,
//   @milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,
//   @milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12
//
// esm.sh filters the deps list per package, so the same string makes crepe
// and kit converge on the SAME @milkdown/core build — and @milkdown/core is
// where the ProseMirror Plugin/Decoration classes the fold plugin uses are
// created. With mismatched deps strings esm.sh would hand the fold plugin a
// SECOND prosemirror instance whose classes differ, and editor.use()/create()
// would throw at build time — which is why createWithFallback() below retries
// once without folding (see prosePluginsSupported).
//
// The list pins the family; the SDK's module snapshot freezes what it
// resolves (module-snapshot/, which the app serves before upstream),
// and `python -m collimate._host.module_mirror bump` moves the versions inside the
// list with the URL pins.
// ---------------------------------------------------------------------------
import React, { useEffect, useLayoutEffect, useRef, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { Crepe } from 'https://esm.sh/@milkdown/crepe@7.22.3?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
// ProseMirror plugin primitives, pulled from the SAME @milkdown/kit@7.22.3
// graph crepe itself imports (see the DEPS note above) — these classes are
// the exact singletons crepe's editor uses.
import { $prose } from 'https://esm.sh/@milkdown/kit@7.22.3/utils?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
import { NodeSelection, Plugin, PluginKey, TextSelection } from 'https://esm.sh/@milkdown/kit@7.22.3/prose/state?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
import { Decoration, DecorationSet } from 'https://esm.sh/@milkdown/kit@7.22.3/prose/view?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
// The Insert action's, the no-uploads wiring's and the embeds' pieces (§3b,
// §3c), from the same graph: the editor's ctx slices (the parser too, for an
// external body applied in place, and the remark plugin list the embeds'
// transform joins), the commonmark nodes and command the insertion writes
// with, Crepe's image-block node the embeds extend, and the upload plugin's
// config Crepe's builder fills.
import { InitReady, commandsCtx, editorViewCtx, editorViewOptionsCtx, parserCtx, remarkPluginsCtx } from 'https://esm.sh/@milkdown/kit@7.22.3/core?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
import { clearTextInCurrentBlockCommand, imageSchema, linkSchema } from 'https://esm.sh/@milkdown/kit@7.22.3/preset/commonmark?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
import { imageBlockSchema } from 'https://esm.sh/@milkdown/kit@7.22.3/component/image-block?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
import { uploadConfig } from 'https://esm.sh/@milkdown/kit@7.22.3/plugin/upload?deps=@milkdown/core@7.22.3,@milkdown/crepe@7.22.3,@milkdown/ctx@7.22.3,@milkdown/kit@7.22.3,@milkdown/plugin-listener@7.22.3,@milkdown/prose@7.22.3,codemirror@6.0.2,prosemirror-model@1.25.12&target=es2022';
// The fenced-code highlighter (§2b) is built from @codemirror/language and
// @lezer/highlight, imported by the SAME specifiers Crepe's CodeMirror graph
// imports them by: codemirror@6.0.2, @codemirror/language-data and
// @codemirror/theme-one-dark import '/@codemirror/language@^6.0.0?target=es2022',
// and @codemirror/language and theme-one-dark import
// '/@lezer/highlight@^1.0.0?target=es2022'. A module is keyed by its URL, so
// these are that graph's own module records: the HighlightStyle registers with
// the facet Crepe's code blocks read, and its tags are the objects the
// language parsers tag their trees with. The canonical deps= list does not
// reach this graph (Crepe's own imports of it are these ranges), so these
// carry none. An exact version here would be a second module record beside
// the graph's own range specifier, and highlighting would silently stop.
// These two follow whatever the frozen graph resolves, and add no drift of
// their own.
import { HighlightStyle, syntaxHighlighting } from 'https://esm.sh/@codemirror/language@^6.0.0?target=es2022';
import { tags } from 'https://esm.sh/@lezer/highlight@^1.0.0?target=es2022';
import {
    defineRenderer,
    mirrorUrl,
    isCellFileDrag,
    readCellFileDrop,
    syntaxPalette,
    useCollimateFile,
    useStyle,
    useTheme,
    StyleControl,
} from '/api/sdk/react.js';
import { CODE_BLOCK_CSS, highlightSpecs, syntaxVars } from './code-highlight';
import { extendImageBlockSchema, extendImageSchema, remarkEmbedAttributes } from './embed-markdown';
import {
    EMBED_HANDLE_WIDTH, EMBED_WIDTH_PRESETS, IMAGE_EXT, applyEmbedLayout, createEmbeds, draggedWidth,
    embedHostsToolbar, embedSizable, embedTargetKey, embedToLink, embedToolbarTarget, linkToEmbed,
    placeEmbedHandle, placeEmbedToolbar, proxiedEmbedUrl, resolveCellPath, resolveEmbed, setEmbedLayout,
} from './embeds';
import { milkdownSnapshotCodec } from './snapshot-codec';
import { restoreProseMirrorSnapshot } from './snapshot-model';
import { transactionTextInput } from './text-input';

// ---------------------------------------------------------------------------
// §2 Crepe's sheets, the skin CSS (tokens, prose, chrome), the style
// ---------------------------------------------------------------------------

// Crepe's theme stylesheet. lib/theme/common/style.css @imports crepe's
// component sheets, and four of those @import BARE package paths
// ('@milkdown/kit/prose/view/style/prosemirror.css',
// 'katex/dist/katex.min.css', …) that only a bundler resolves: on the CDN they
// 404 as text/plain, the browser blocks them, and ProseMirror's base styles,
// the gap cursor, the table base and KaTeX go unstyled. So the renderer builds
// the same cascade from files that exist: crepe's component sheets in
// style.css's own @import order, each bare import replaced by its resolved,
// version-pinned sheet at the same position. Each sheet is fetched from the
// app's module mirror (mirrorUrl; mirror_assets.json names the sheets and
// KaTeX's fonts), its @import lines dropped and its relative url()s made
// absolute against the mirrored sheet, and the whole cascade is injected as
// one stylesheet. The
// prosemirror-virtual-cursor sheet crepe's cursor.css also imports is left out:
// this renderer switches the virtual cursor off (see the injected CSS below).
const CREPE_THEME = 'https://cdn.jsdelivr.net/npm/@milkdown/crepe@7.22.3/lib/theme/common';
const PROSE_STYLE = 'https://cdn.jsdelivr.net/npm/@milkdown/prose@7.22.3/lib/style';
const CREPE_THEME_SHEETS = [
    `${PROSE_STYLE}/prosemirror.css`,   // crepe's prosemirror.css is only this import
    `${CREPE_THEME}/reset.css`,
    `${CREPE_THEME}/block-edit.css`,
    `${CREPE_THEME}/code-mirror.css`,
    `${PROSE_STYLE}/gapcursor.css`,     // imported by crepe's cursor.css
    `${CREPE_THEME}/cursor.css`,
    `${CREPE_THEME}/image-block.css`,
    `${CREPE_THEME}/link-tooltip.css`,
    `${CREPE_THEME}/list-item.css`,
    `${CREPE_THEME}/placeholder.css`,
    `${CREPE_THEME}/toolbar.css`,
    `${PROSE_STYLE}/tables.css`,        // imported by crepe's table.css
    `${CREPE_THEME}/table.css`,
    'https://cdn.jsdelivr.net/npm/katex@0.19.1/dist/katex.min.css', // imported by crepe's latex.css
    `${CREPE_THEME}/latex.css`,
    `${CREPE_THEME}/top-bar.css`,
    `${CREPE_THEME}/diff.css`,
    `${CREPE_THEME}/ai.css`,
];

async function crepeThemeCss(): Promise<string> {
    const sheets = await Promise.all(CREPE_THEME_SHEETS.map(async (href) => {
        const mirrored = mirrorUrl(href);
        try {
            const res = await fetch(mirrored);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return (await res.text())
                .replace(/@import\s+[^;]+;/g, '')
                .replace(
                    /url\(\s*(['"]?)(?!data:|https?:|\/\/|#)([^'")]+)\1\s*\)/g,
                    (_m, q, ref) => `url(${q}${new URL(ref, mirrored).href}${q})`,
                );
        } catch (e) {
            // A missing sheet degrades that part of the theme; the editor
            // still renders.
            console.warn('milkdown: theme sheet failed to load', href, e);
            return '';
        }
    }));
    return sheets.join('\n');
}

// The editor's whole look, in pure CSS on the skin tokens. The host mirrors
// every skin token onto this frame's :root under its CSS name (--sheet,
// --prose-ink, --font-prose, …) and repaints them on a skin change, so nothing
// here knows a scheme or a colour: the frame re-skins without re-injecting.
//
// The prose and the chrome read their grounds, inks, surfaces and accents
// through the --crepe-* variables, never the skin tokens directly: under the
// skin those are the tokens (CREPE_TOKEN_CSS), and a library theme the user
// picks in the style control (Classic, Frame, Nord: §2's library themes)
// redeclares them above the map, so it recolours the prose, the chrome and
// the ground at once, and CODE_BLOCK_CREPE_CSS carries the fenced code
// block along. Hairlines (--line, --line-2) and the live accent (--teal)
// have no Crepe variable and stay the skin's.
//
// CREPE_TOKEN_CSS maps each --crepe-* variable Crepe's sheets read onto a
// skin token. Brass is the selection (text selection, selected nodes); teal
// is live or an on-state (the caret, an active toolbar mark, a focused
// field, a link's address); red is an error. The two diff colours are declared by
// crepe's diff.css on .milkdown itself, so they are mapped there.
export const CREPE_TOKEN_CSS = `
:root {
    --crepe-color-background: var(--sheet);
    --crepe-color-on-background: var(--prose-ink);
    --crepe-color-surface: var(--sheet-2);
    --crepe-color-surface-low: var(--sheet-3);
    --crepe-color-on-surface: var(--ink);
    --crepe-color-on-surface-variant: var(--ink-2);
    --crepe-color-outline: var(--ink-3);
    --crepe-color-primary: var(--brass);
    --crepe-color-secondary: var(--sheet-3);
    --crepe-color-on-secondary: var(--ink);
    --crepe-color-inverse: var(--sheet-3);
    --crepe-color-on-inverse: var(--ink);
    --crepe-color-inline-code: var(--prose-ink);
    --crepe-color-error: var(--red);
    --crepe-color-hover: var(--sheet-3);
    --crepe-color-selected: var(--brass-soft);
    --crepe-color-inline-area: var(--code-bg);
    --crepe-font-title: var(--font-prose);
    --crepe-font-default: var(--font-ui);
    --crepe-font-code: var(--font-mono);
    --crepe-shadow-1: var(--shadow);
    --crepe-shadow-2: var(--shadow);
}
:root .milkdown {
    --crepe-color-diff-added: var(--green);
    --crepe-color-diff-removed: var(--red);
}
`;

// Fenced code under a library theme. CODE_BLOCK_CSS (code-highlight.ts)
// paints the block and CodeMirror's surfaces on the skin's tokens; this layer, one :root above each of its selectors, repaints
// the colours that have a Crepe variable through that variable, so Classic,
// Frame or Nord recolour the code block with the rest of the document. Under
// the skin each variable resolves to the token the shared sheet names
// (CREPE_TOKEN_CSS), so nothing changes there. Hairlines, teal, the soft
// accents and the syntax palette (no Crepe variable) stay the skin's.
export const CODE_BLOCK_CREPE_CSS = `
:root .milkdown .ProseMirror .milkdown-code-block { background: var(--crepe-color-inline-area); color: var(--crepe-color-on-surface-variant); }
:root .milkdown .milkdown-code-block .cm-gutters { color: var(--crepe-color-outline); }
:root .milkdown .milkdown-code-block .cm-content { color: var(--crepe-color-on-surface-variant); }
:root .milkdown .milkdown-code-block .cm-selectionBackground,
:root .milkdown .milkdown-code-block .cm-content ::selection { background: var(--crepe-color-selected) !important; }
:root .milkdown .milkdown-code-block .cm-foldPlaceholder { background: var(--crepe-color-surface-low); color: var(--crepe-color-outline); }
:root .milkdown .milkdown-code-block .cm-tooltip,
:root .milkdown .milkdown-code-block .cm-panels { background: var(--crepe-color-surface); color: var(--crepe-color-on-surface); }
:root .milkdown .milkdown-code-block .cm-tooltip-autocomplete ul li[aria-selected] { background: var(--crepe-color-selected); color: var(--crepe-color-on-surface); }
:root .milkdown .milkdown-code-block .cm-tooltip-autocomplete-disabled ul li[aria-selected] { background: var(--crepe-color-surface-low); color: var(--crepe-color-on-surface-variant); }
:root .milkdown .milkdown-code-block .cm-textfield { background: var(--crepe-color-surface-low); color: var(--crepe-color-on-surface); }
`;

// The document's type: the mock's .md block (night-workbench-mock.html,
// "markdown renderer"), on ProseMirror's DOM. Crepe's reset.css sizes
// headings and paragraphs in absolute px with their own margins; every such
// rule is overridden here at a higher specificity. The prose face is set to a
// 0.475 x-height (font-size-adjust) so it reads at the size it was tuned at;
// the sans and mono inside a document keep their own. The prose face is the
// family the user chose in the style control (--colmd-font, set by the
// editor), else the skin's.
const PROSE_CSS = `
html, body { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background: var(--crepe-color-background); }

/* .milkdown is the content shell: the sheet ground reaches below the last
   paragraph instead of clinging to the text. */
.milkdown { background: var(--crepe-color-background); min-height: 100%; }

.milkdown > .ProseMirror {
    padding: 1.625rem 1.875rem 1.875rem;
    min-height: 100%;
    outline: none;
    font-family: var(--colmd-font, var(--font-prose));
    color: var(--crepe-color-on-background);
    font-size: 0.96875rem;
    line-height: 1.55;
    font-size-adjust: 0.475;
}

.milkdown .ProseMirror h1,
.milkdown .ProseMirror h2,
.milkdown .ProseMirror h3,
.milkdown .ProseMirror h4,
.milkdown .ProseMirror h5,
.milkdown .ProseMirror h6 { font-family: var(--colmd-font, var(--font-prose)); padding: 0; }
.milkdown .ProseMirror h1 { font-size: 1.5rem; font-weight: 600; letter-spacing: -0.01em; line-height: 1.2; margin-top: 1.375rem; }
.milkdown .ProseMirror h2 {
    font-family: var(--font-ui);
    font-size: 0.8125rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--crepe-color-on-surface-variant);
    line-height: 1.55;
    margin-top: 1.375rem;
    margin-bottom: 0.5rem;
    font-size-adjust: none;
}
.milkdown .ProseMirror h3 { font-size: 1rem; font-weight: 600; line-height: 1.3; margin-top: 1.125rem; }
.milkdown .ProseMirror h4,
.milkdown .ProseMirror h5,
.milkdown .ProseMirror h6 { font-size: 1em; font-weight: 600; line-height: 1.55; margin-top: 0.875rem; }
.milkdown .ProseMirror h5,
.milkdown .ProseMirror h6 { color: var(--crepe-color-on-surface-variant); }

.milkdown .ProseMirror p { font-size: inherit; line-height: inherit; padding: 0; margin-top: 0.625rem; }
.milkdown .ProseMirror strong { font-weight: 600; }

/* Lists. Crepe draws each item's marker in a label column beside the text,
   so the column and its gap make the mock's 22px indent (+2px item padding). */
.milkdown .ProseMirror ul,
.milkdown .ProseMirror ol { margin: 0.625rem 0 0; padding: 0; }
.milkdown .ProseMirror .milkdown-list-item-block { margin-top: 0.3125rem; }
.milkdown .ProseMirror .milkdown-list-item-block li { gap: 0.375rem; }
.milkdown .ProseMirror .milkdown-list-item-block li .label-wrapper {
    width: 1.125rem;
    height: 1.55em;
    flex: none;
    justify-content: flex-end;
    color: var(--crepe-color-outline);
}
.milkdown .ProseMirror .milkdown-list-item-block li .label-wrapper svg { width: 1.125rem; height: 1.125rem; fill: var(--crepe-color-outline); }
.milkdown .ProseMirror .milkdown-list-item-block li .label-wrapper .label {
    width: auto;
    height: 1.55em;
    padding: 0;
    white-space: nowrap;
}
.milkdown .ProseMirror li p:first-child,
.milkdown .ProseMirror blockquote p:first-child,
.milkdown .ProseMirror th p:first-child,
.milkdown .ProseMirror td p:first-child { margin-top: 0; }

.milkdown .ProseMirror a {
    color: inherit;
    text-decoration: underline;
    text-decoration-color: var(--line-2);
    text-decoration-thickness: 1px;
    text-underline-offset: 0.1875rem;
}
.milkdown .ProseMirror a:hover { text-decoration-color: var(--crepe-color-on-surface-variant); }

.milkdown .ProseMirror blockquote {
    margin: 0.875rem 0 0;
    padding: 0.0625rem 0 0.0625rem 0.875rem;
    box-sizing: border-box;
    border-left: 2px solid var(--line-2);
    color: var(--crepe-color-on-surface-variant);
}
.milkdown .ProseMirror blockquote::before { content: none; }

.milkdown .ProseMirror code {
    font-family: var(--font-mono);
    font-size: 0.8em;
    padding: 0.0625rem 0.25rem;
    border: 1px solid var(--line);
    border-radius: 0.25rem;
    background: var(--crepe-color-inline-area);
    color: inherit;
    display: inline;
    line-height: inherit;
    font-size-adjust: none;
}
/* An inline formula is document text: Crepe's latex.css paints it
   --crepe-color-primary (brass, the selection accent), so it inherits. */
.milkdown .ProseMirror span[data-type='math_inline'] { color: inherit; }
.milkdown .ProseMirror pre {
    margin: 0.75rem 0 0;
    padding: 0.625rem 0.75rem;
    border: 1px solid var(--line);
    border-radius: 0.375rem;
    background: var(--crepe-color-inline-area);
    font-family: var(--font-mono);
    font-size: 0.71875rem;
    line-height: 1.55;
    color: var(--crepe-color-on-surface-variant);
    white-space: pre-wrap;
    font-size-adjust: none;
}
.milkdown .ProseMirror pre code { padding: 0; border: 0; background: none; font-size: inherit; }

${CODE_BLOCK_CSS}
${CODE_BLOCK_CREPE_CSS}

/* Tables: rules, no grid. */
.milkdown .ProseMirror .milkdown-table-block { margin: 0.75rem 0 0; }
.milkdown .ProseMirror table { width: 100%; border-collapse: collapse; font-size: 0.92em; }
.milkdown .ProseMirror .milkdown-table-block th {
    padding: 0.375rem 0.75rem 0.375rem 0;
    border: 0;
    border-bottom: 1px solid var(--line-2);
    text-align: left;
    font-family: var(--font-ui);
    font-size: 0.71875rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--crepe-color-on-surface-variant);
    font-size-adjust: none;
}
.milkdown .ProseMirror .milkdown-table-block td {
    padding: 0.4375rem 0.75rem 0.4375rem 0;
    border: 0;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
}
.milkdown .ProseMirror .column-resize-handle { background-color: var(--crepe-color-primary); }

.milkdown .ProseMirror hr {
    margin: 1.375rem 0 0;
    padding: 0;
    height: 0;
    border: 0;
    border-top: 1px solid var(--line);
    background: none;
}
.milkdown .ProseMirror hr.ProseMirror-selectednode { background: none; border-top-color: var(--crepe-color-primary); }
.milkdown .ProseMirror hr.ProseMirror-selectednode::before { content: none; }
`;

// Crepe's floating chrome (selection toolbar, slash menu, link preview and
// edit, the code block's language list and tools, table handles, image and
// math controls) in the app's menu vocabulary: a --sheet-2 plate with a
// --line-2 edge and the skin's shadow, --ink text, --ink-3 icons, --sheet-3
// hover, the brass-soft fill on the chosen row, teal for an active mark or a
// focused field. All of it is interface: the UI face, no prose x-height.
const CHROME_CSS = `
.milkdown .milkdown-toolbar,
.milkdown .milkdown-slash-menu,
.milkdown .milkdown-link-preview > .link-preview,
.milkdown .milkdown-link-edit > .link-edit,
.milkdown .milkdown-latex-inline-edit,
.milkdown .milkdown-code-block .list-wrapper,
.milkdown .milkdown-table-block .cell-handle .button-group {
    background: var(--crepe-color-surface);
    color: var(--crepe-color-on-surface);
    border: 1px solid var(--line-2);
    border-radius: var(--radius);
    box-shadow: var(--crepe-shadow-1);
    font-family: var(--font-ui);
    font-size-adjust: none;
}

.milkdown .milkdown-toolbar .divider { background: var(--line-2); height: 1.125rem; margin: 0.5625rem 0.25rem; }
.milkdown .milkdown-toolbar .toolbar-item { width: 1.75rem; height: 1.75rem; margin: 0.25rem 0.125rem; padding: 0.25rem; border-radius: 0.3125rem; }
.milkdown .milkdown-toolbar .toolbar-item svg { width: 1.25rem; height: 1.25rem; color: var(--crepe-color-on-surface-variant); fill: var(--crepe-color-on-surface-variant); }
.milkdown .milkdown-toolbar .toolbar-item:hover svg { color: var(--crepe-color-on-surface); fill: var(--crepe-color-on-surface); }
.milkdown .milkdown-toolbar .toolbar-item.active svg { color: var(--teal); fill: var(--teal); }

.milkdown .milkdown-slash-menu { padding: 0.25rem; }
.milkdown .milkdown-slash-menu .tab-group { padding: 0 0 0.25rem; border-bottom: 1px solid var(--line); }
.milkdown .milkdown-slash-menu .tab-group ul { padding: 0.125rem; gap: 0.125rem; }
.milkdown .milkdown-slash-menu .tab-group ul li {
    padding: 0 0.625rem;
    font-size: 0.78125rem;
    font-weight: 500;
    line-height: 1.625rem;
    color: var(--crepe-color-on-surface-variant);
    border-radius: 0.3125rem;
}
.milkdown .milkdown-slash-menu .tab-group ul li:hover,
.milkdown .milkdown-slash-menu .tab-group ul li.selected { color: var(--crepe-color-on-surface); }
.milkdown .milkdown-slash-menu .menu-groups { padding: 0; }
.milkdown .milkdown-slash-menu .menu-groups .menu-group h6 {
    padding: 0.5rem 0.625rem 0.3125rem;
    font-size: 0.6875rem;
    font-weight: 600;
    line-height: 1.5;
    letter-spacing: 0.08em;
    color: var(--crepe-color-outline);
}
.milkdown .milkdown-slash-menu .menu-groups .menu-group li {
    min-width: 14.75rem;
    height: 1.875rem;
    padding: 0 0.625rem;
    gap: 0.625rem;
    border-radius: 0.375rem;
    color: var(--crepe-color-on-surface-variant);
}
.milkdown .milkdown-slash-menu .menu-groups .menu-group li > span { font-size: 0.8125rem; font-weight: 400; line-height: 1.875rem; }
.milkdown .milkdown-slash-menu .menu-groups .menu-group li svg { width: 1rem; height: 1rem; color: var(--crepe-color-outline); fill: var(--crepe-color-outline); }
.milkdown .milkdown-slash-menu .menu-groups .menu-group li.hover,
.milkdown .milkdown-slash-menu .menu-groups .menu-group li.active { color: var(--crepe-color-on-surface); }
.milkdown .milkdown-slash-menu .menu-groups .menu-group li.active svg { color: var(--crepe-color-primary); fill: var(--crepe-color-primary); }
.milkdown .milkdown-slash-menu .menu-groups .menu-group + .menu-group::before { background: var(--line); margin: 0.25rem 0.375rem; }

.milkdown .milkdown-link-preview > .link-preview > .link-display {
    color: var(--teal);
    font-family: var(--font-mono);
    font-size: 0.75rem;
}
.milkdown .milkdown-link-edit > .link-edit > .input-area {
    color: var(--crepe-color-on-surface);
    font-family: var(--font-mono);
    font-size: 0.75rem;
    caret-color: var(--teal);
}

.milkdown .milkdown-code-block .tools .language-button,
.milkdown .milkdown-code-block .tools .tools-button-group button {
    background: var(--crepe-color-surface);
    color: var(--crepe-color-on-surface-variant);
    border: 1px solid var(--line-2);
    font-family: var(--font-ui);
    font-size: 0.71875rem;
    font-weight: 500;
}
/* The language picker and copy button float in the block's top-right
   corner (shown on hover) instead of taking a row above the code. */
.milkdown .milkdown-code-block .tools { position: absolute; top: 0.375rem; right: 0.5rem; z-index: 2; gap: 0.375rem; }
.milkdown .milkdown-code-block .tools .language-button { border-radius: 0.3125rem; margin-bottom: 0; }
.milkdown .milkdown-code-block .tools .language-button:hover,
.milkdown .milkdown-code-block .tools .tools-button-group button:hover { background: var(--crepe-color-hover); color: var(--crepe-color-on-surface); }
.milkdown .milkdown-code-block .list-wrapper { padding: 0.25rem; }
.milkdown .milkdown-code-block .language-list-item {
    height: 1.875rem;
    padding: 0 0.625rem;
    border-radius: 0.375rem;
    font-size: 0.8125rem;
    font-weight: 400;
    color: var(--crepe-color-on-surface-variant);
}
.milkdown .milkdown-code-block .language-list-item:hover,
.milkdown .milkdown-code-block .language-list-item:focus-visible { color: var(--crepe-color-on-surface); }
.milkdown .milkdown-code-block .search-box { margin: 0 0 0.25rem; outline: 1px solid var(--line-2); border-radius: 0.375rem; }
.milkdown .milkdown-code-block .search-box:has(input:focus) { outline: 1px solid var(--teal); }
.milkdown .milkdown-code-block .search-box input { font-family: var(--font-ui); font-size: 0.8125rem; color: var(--crepe-color-on-surface); }
.milkdown .milkdown-code-block .search-box .clear-icon svg { color: var(--crepe-color-outline); fill: var(--crepe-color-outline); }

.milkdown .milkdown-table-block .cell-handle,
.milkdown .milkdown-table-block .line-handle .add-button { border: 1px solid var(--line-2); }

.milkdown .milkdown-image-block .image-edit,
.milkdown .milkdown-image-inline .empty-image-inline {
    border: 1px solid var(--line-2);
    border-radius: 0.375rem;
    font-family: var(--font-ui);
    font-size-adjust: none;
}
.milkdown .milkdown-image-block .caption-input { font-family: var(--font-ui); font-size: 0.75rem; color: var(--crepe-color-on-surface-variant); font-size-adjust: none; }
/* No uploads (§3b): an empty image keeps its link field (a URL or a path in
   this cell, typed by hand) but never offers Crepe's Upload control. */
.milkdown .milkdown-image-block .image-edit .placeholder .uploader,
.milkdown .milkdown-image-inline .image-edit .placeholder .uploader { display: none !important; }
`;

// The editor's own behaviour: the text size, the hidden block handle and
// placeholder, heading folding, the native caret in place of Crepe's, and
// the embeds' sizing inside Crepe's image views.
const EDITOR_CSS = `
/* Text size. zoom (not font-size) so it scales every content element
   uniformly. Chromium reports post-zoom rects, so crepe's floating chrome
   (slash menu, toolbar) still positions correctly. The fallback is the
   default size until the editor sets the var from the style. */
.milkdown .ProseMirror { zoom: var(--colmd-zoom, 1); }

/* The left block handle (the "+" / drag grip that tracks the mouse down the
   left margin) is hidden. The slash menu (type "/") still works; it is a
   separate widget. */
.milkdown .milkdown-block-handle { display: none !important; }

/* Crepe's "Please enter…" placeholder. The feature is also disabled at
   construction; this hides the decoration's ::before as well. */
.milkdown .ProseMirror .crepe-placeholder::before { content: none !important; }

/* Obsidian-style heading fold control. The chevron is a real <span> WIDGET
   decoration (not a pseudo) so it owns its own click handler: toggling is
   independent of the text size and needs no clientX math. It sits in the
   30px left padding, its hit area flush with the heading text. It shows only
   while the heading row is hovered or holds the caret (.colmd-active), and
   stays visible (rotated) while collapsed. */
.milkdown .ProseMirror .colmd-foldable { position: relative; }
.milkdown .ProseMirror .colmd-fold-toggle {
    position: absolute;
    left: -1.875rem;
    top: 0;
    width: 1.875rem;
    height: 1.3em;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding-left: 0.375rem;
    box-sizing: border-box;
    opacity: 0;
    transition: opacity 0.12s ease;
    cursor: pointer;
    user-select: none;
}
.milkdown .ProseMirror .colmd-fold-toggle::before {
    content: '';
    width: 0;
    height: 0;
    border-style: solid;
    border-width: 0.375rem 0.25rem 0 0.25rem;
    border-color: var(--ink-3) transparent transparent transparent;
}
.milkdown .ProseMirror .colmd-foldable:hover > .colmd-fold-toggle,
.milkdown .ProseMirror .colmd-foldable.colmd-active > .colmd-fold-toggle { opacity: 0.6; }
.milkdown .ProseMirror .colmd-foldable.colmd-folded > .colmd-fold-toggle { opacity: 0.9; }
.milkdown .ProseMirror .colmd-fold-toggle:hover { opacity: 1; }
.milkdown .ProseMirror .colmd-foldable.colmd-folded > .colmd-fold-toggle::before {
    border-width: 0.25rem 0 0.25rem 0.375rem;
    border-color: transparent transparent transparent var(--ink-3);
}

/* The "…" trailing a collapsed heading, signalling hidden content. A
   clickable widget (unfolds on click). */
.milkdown .ProseMirror .colmd-fold-ellipsis {
    margin-left: 0.4em;
    color: var(--ink-3);
    cursor: pointer;
    user-select: none;
}
.milkdown .ProseMirror .colmd-fold-ellipsis::before { content: '…'; }
.milkdown .ProseMirror .colmd-fold-ellipsis:hover { color: var(--ink-2); }

/* The first block sits flush with the top padding: its heading or paragraph
   margin would otherwise show as an extra line above the first line. The
   sibling selectors cover the moment a selection starts, when
   prosemirror-virtual-cursor inserts a zero-height div as the actual first
   DOM child and knocks the block out of :first-child (CSS :first-child
   matches DOM order, ignoring display), which made the document jump. */
.milkdown .ProseMirror > *:first-child,
.milkdown .ProseMirror > .prosemirror-virtual-cursor + *,
.milkdown .ProseMirror > .ProseMirror-widget:first-child + * { margin-top: 0; }

/* prosemirror-virtual-cursor is off. Its -left / -right variants paint
   L-shaped overlays that dangle above the caret by 1ch x line-height, which
   showed as a phantom line above the first line. The native caret, in teal,
   does everything it did here. */
.milkdown .ProseMirror,
.milkdown .ProseMirror.virtual-cursor-enabled { caret-color: var(--teal) !important; }
.prosemirror-virtual-cursor,
.prosemirror-virtual-cursor-animation,
.prosemirror-virtual-cursor-left,
.prosemirror-virtual-cursor-right { display: none !important; }

/* ProseMirror's gap cursor would draw a short bar above the first block
   (non-text entry points); a markdown editor does not need it. */
.milkdown .ProseMirror-gapcursor,
.milkdown .ProseMirror-focused .ProseMirror-gapcursor { display: none !important; }

/* Embeds (§3c). An image's size is its width in the markdown
   ({width=NN%}), so Crepe's height-ratio handle is hidden and an image keeps
   its aspect (its height follows its width, over the height Crepe sets on
   load). A sized embed (data-embed-width on its view) fills its box, which
   the view sizes. */
.milkdown .milkdown-image-block > .image-wrapper > .image-resize-handle { display: none !important; }
.milkdown .milkdown-image-block > .image-wrapper img { height: auto !important; min-height: 0; }
.milkdown .colmd-embed .milkdown-image-inline img { max-width: 100%; }
.milkdown .colmd-embed[data-embed-width] .milkdown-image-block > .image-wrapper { width: 100%; min-width: 0; }
.milkdown .colmd-embed[data-embed-width] .milkdown-image-block > .image-wrapper img { width: 100%; }
.milkdown .colmd-embed[data-embed-width] .milkdown-image-inline { display: flex; width: 100%; }
.milkdown .colmd-embed[data-embed-width] .milkdown-image-inline img { width: 100%; height: auto; }
/* A selected embed shows one mark, the view's brass ring on its box, for an
   image as for a video: Crepe's own (an inline image's outline, a block
   image's tint) is off. */
.milkdown .colmd-embed .milkdown-image-inline.selected { outline: none; }
.milkdown .colmd-embed .milkdown-image-block.selected > .image-wrapper::before { content: none; }
`;

/** The editor's box: it fills the renderer's box. */
export const EDITOR_ROOT = Object.freeze({
    position: 'relative', width: '100%', height: '100%', overflow: 'hidden',
});

/** Everything the frame injects after Crepe's own sheets: the token map, the
 *  mock's prose, the chrome, and the editor behaviour, in that order. */
export function milkdownCss(): string {
    return [CREPE_TOKEN_CSS, PROSE_CSS, CHROME_CSS, EDITOR_CSS].join('\n');
}

// ---------------------------------------------------------------------------
// §2b Fenced code: the skin's syntax palette on Crepe's CodeMirror
// ---------------------------------------------------------------------------
//
// A fenced block is a CodeMirror editor that Crepe's code-mirror feature
// builds from that feature's config: `theme` (Crepe's default is One Dark,
// a palette of its own) and `extensions`, appended after it. The renderer
// passes `theme: null`, which leaves One Dark out (Crepe merges its defaults
// in with lodash's defaultsDeep, which keeps a null), and one extension,
// syntaxHighlighting(skinHighlightStyle()). code-highlight.ts holds the
// Lezer tag map, the highlight's specs and the block's CSS (CODE_BLOCK_CSS,
// in PROSE_CSS).
//
// The HighlightStyle names no colour of its own: each tag's colour is
// var(--colmd-syntax-<role>), and applyTheme writes those properties on
// :root from syntaxPalette(theme), the SDK's palette Monaco draws code in
// too, on every skin record. So the style is built once, and every block,
// open or added later, follows a skin change with no reconfiguration.
// Before a readable record (syntaxPalette returns null) the properties are
// unset: the colour is then invalid at computed-value time and falls back
// to inheriting, so code reads in the block's ink until the skin arrives.
export {
    CODE_BLOCK_CSS, LEZER_TAG_ROLES, highlightSpecs, lezerTag, syntaxVar, syntaxVars,
} from './code-highlight';

// Built on first use, not at module load: the editor is what needs it.
let skinHighlight: any = null;
function skinHighlightStyle() {
    if (!skinHighlight) skinHighlight = HighlightStyle.define(highlightSpecs(tags));
    return skinHighlight;
}

// The record's scheme drives native controls (scrollbars, form widgets) in
// the frame; the skin id is already on data-theme from the host. A library
// theme follows the same scheme (its light or dark sheet, libraryThemeUrl),
// so the frame's color-scheme is the skin's whatever the choice. The syntax
// palette is written as the --colmd-syntax-* properties the code blocks'
// highlight reads.
function applyTheme(theme: { scheme: string }) {
    const root = document.documentElement;
    root.style.colorScheme = theme.scheme;
    for (const [name, value] of Object.entries(syntaxVars(syntaxPalette(theme as any)))) {
        if (value) root.style.setProperty(name, value);
        else root.style.removeProperty(name);
    }
}

// The style (the style control's three rows; the host keeps the record per
// channel under this renderer's id, `useStyle(api)`).
//
// Text size: the record's factor (null: the default, 100%) on --colmd-zoom,
// which EDITOR_CSS's zoom rule reads.
export const TEXT_SIZE = Object.freeze({ min: 0.7, max: 2.2, step: 0.1, default: 1 });

/** The --colmd-zoom factor for a stored text size: the default for null (or
 *  anything that is not a number), else the value inside the range. */
export function textSizeZoom(textSize) {
    const value = typeof textSize === 'number' && Number.isFinite(textSize) ? textSize : TEXT_SIZE.default;
    return Math.min(TEXT_SIZE.max, Math.max(TEXT_SIZE.min, Math.round(value * 100) / 100));
}

// Theme: the skin (null, CREPE_TOKEN_CSS) or one of Crepe's own families.
// Each family follows the skin's scheme: Crepe ships a light and a
// dark sheet per family (crepe/crepe-dark is the family Crepe calls its own,
// "Classic" here; frame/frame-dark; nord/nord-dark), fetched from the same
// pinned root as Crepe's common sheets. A sheet is one `.milkdown { … }`
// block of --crepe-* declarations; liftLibraryTheme moves it to
// `:root:root`, which outranks the token map's `:root` and reaches the html
// and body grounds, and the editor injects it into one replaceable <style>.
export const MILKDOWN_THEMES = Object.freeze([
    Object.freeze({ id: 'skin', label: 'Collimate' }),
    Object.freeze({ id: 'classic', label: 'Classic' }),
    Object.freeze({ id: 'frame', label: 'Frame' }),
    Object.freeze({ id: 'nord', label: 'Nord' }),
]);
const LIBRARY_THEME_DIRS = new Map([['classic', 'crepe'], ['frame', 'frame'], ['nord', 'nord']]);
const CREPE_THEME_ROOT = CREPE_THEME.slice(0, CREPE_THEME.lastIndexOf('/'));
/** The id of the <style> a library theme is injected into. */
export const LIBRARY_THEME_STYLE_ID = 'colmd-library-theme';

/** The sheet for a stored theme id under the skin's scheme, or null for the
 *  skin (null, 'skin' or an id this renderer does not offer). */
export function libraryThemeUrl(themeId, scheme) {
    const dir = typeof themeId === 'string' ? LIBRARY_THEME_DIRS.get(themeId) : undefined;
    if (!dir) return null;
    return `${CREPE_THEME_ROOT}/${scheme === 'light' ? dir : `${dir}-dark`}/style.css`;
}

/** A library sheet as the editor injects it: its `.milkdown` block lifted to
 *  `:root:root`, and without its --crepe-font-* declarations, whose faces
 *  the frame does not load: the prose face stays the style's font, else the
 *  skin's. */
export function liftLibraryTheme(css) {
    return String(css)
        .replace(/\.milkdown\s*\{/g, ':root:root {')
        .replace(/--crepe-font-[\w-]+\s*:[^;]*;/g, '');
}

const libraryThemeSheets = new Map();
/** The lifted sheet at `href`, fetched once per frame from the app's module
 *  mirror. */
function libraryThemeCss(href) {
    if (!libraryThemeSheets.has(href)) {
        libraryThemeSheets.set(href, fetch(mirrorUrl(href))
            .then((res) => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.text();
            })
            .then(liftLibraryTheme)
            .catch((e) => {
                libraryThemeSheets.delete(href);
                throw e;
            }));
    }
    return libraryThemeSheets.get(href);
}

/** Put `css` in the library theme's <style>, or remove it for null. */
function setLibraryThemeSheet(css) {
    let el = document.getElementById(LIBRARY_THEME_STYLE_ID);
    if (css == null) {
        if (el) el.remove();
        return;
    }
    if (!el) {
        el = document.createElement('style');
        el.id = LIBRARY_THEME_STYLE_ID;
        document.head.appendChild(el);
    }
    el.textContent = css;
}

/** The CSS stack for a stored font family, from the host's catalogue; null
 *  for no choice or a family the catalogue no longer has (the skin's face). */
export function fontStack(family, families) {
    if (!family) return null;
    const hit = (families || []).find((f) => f && f.family === family);
    return hit && hit.stack ? hit.stack : null;
}

// ---------------------------------------------------------------------------
// §3 Obsidian-style heading folding.
//
// Implemented as a ProseMirror plugin that paints DECORATIONS, never by
// mutating the editor DOM by hand: PM owns the contentEditable and its own
// DOM observer would treat a stray `display:none`/`data-*` we wrote as a
// foreign change, redraw the node, and wipe it (then our re-apply fights
// back — a flicker loop). Node decorations are applied by PM during its own
// render, so they're stable across edits and never trigger that fight.
//
// Fold state is a Set of "<level>|<heading text>" keys, kept in a React ref
// so it is retained through live editor content updates (FILES_CHANGED / SET_CONTENT)
// — fold the same headings back as long as their text is unchanged. Keying by
// text (not position) is what makes folds stick across an external edit that
// shifts positions; two headings with identical text fold together, an
// acceptable edge.
// ---------------------------------------------------------------------------
function foldKey(level, text) {
    return level + '|' + (text || '').replace(/\s+/g, ' ').trim().slice(0, 240);
}

// Our own ProseMirror plugins (folding, the local-edit note) are strictly
// additive: they depend on esm.sh handing us the SAME prosemirror-state/view
// instance crepe loaded (see the DEPS note up top). If it ever didn't hold, a
// mismatched Plugin/Decoration would throw at editor-create time and take the
// whole editor down. This flag flips off the first time either fails so every
// (re)build after that comes up as a plain crepe editor.
let prosePluginsSupported = true;

// One plugin instance and key per live Crepe editor, retained when content
// replacement recreates its editor state.
//
// Per top-level heading we add:
//   • a node decoration carrying colmd-foldable [+colmd-folded] [+colmd-active]
//     so CSS can scope the chevron's reveal/rotation and the "…" indicator;
//   • a WIDGET decoration in the left gutter — a real <span> chevron that owns
//     its own click handler, so toggling is zoom-independent (no clientX math);
//   • when folded: a trailing WIDGET "…" (clickable, unfolds) plus display:none
//     node decorations on every block down to the next heading of equal-or-
//     higher rank (Obsidian's fold range).
// colmd-active = the caret sits inside this heading. That, or hovering the
// heading row, is the ONLY thing that reveals the chevron — hovering the body
// blocks beneath the heading does not.
function createFoldPlugin(foldStateRef) {
    const key = new PluginKey('colmd-fold');

    // Toggle the fold for whichever heading owns the clicked widget. Resolves
    // the heading from the live doc via getPos(), so there's no stale closure
    // even after edits shift positions.
    const toggleFromWidget = (view, getPos) => {
        const pos = getPos();
        if (pos == null) return;
        let heading;
        try { heading = view.state.doc.resolve(pos).parent; } catch { return; }
        if (!heading || heading.type.name !== 'heading') return;
        const fkey = foldKey(heading.attrs.level || 1, heading.textContent);
        const set = foldStateRef.current;
        if (set.has(fkey)) set.delete(fkey); else set.add(fkey);
        view.dispatch(view.state.tr.setMeta(key, true));
    };

    const widget = (className) => (view, getPos) => {
        const el = document.createElement('span');
        el.className = className;
        el.setAttribute('contenteditable', 'false');
        el.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleFromWidget(view, getPos);
        });
        return el;
    };

    const build = (state) => {
        const { doc, selection } = state;
        const head = selection.head;
        const tops = [];
        doc.forEach((node, offset) => { tops.push({ node, offset }); });
        const decos = [];
        for (let i = 0; i < tops.length; i++) {
            const { node, offset } = tops[i];
            if (node.type.name !== 'heading') continue;
            const level = node.attrs.level || 1;
            const folded = foldStateRef.current.has(foldKey(level, node.textContent));
            const active = head > offset && head < offset + node.nodeSize;
            const cls = ['colmd-foldable'];
            if (folded) cls.push('colmd-folded');
            if (active) cls.push('colmd-active');
            decos.push(Decoration.node(offset, offset + node.nodeSize, { class: cls.join(' ') }));
            // Gutter chevron — present on every heading, revealed by CSS.
            decos.push(Decoration.widget(offset + 1, widget('colmd-fold-toggle'),
                { side: -1, ignoreSelection: true, key: 'colmd-fold-toggle' }));
            if (!folded) continue;
            // Trailing "…" indicator (clickable to unfold), then hide the range.
            decos.push(Decoration.widget(offset + node.nodeSize - 1, widget('colmd-fold-ellipsis'),
                { side: 1, ignoreSelection: true, key: 'colmd-fold-ellipsis' }));
            for (let j = i + 1; j < tops.length; j++) {
                const sib = tops[j];
                if (sib.node.type.name === 'heading' && (sib.node.attrs.level || 1) <= level) break;
                decos.push(Decoration.node(sib.offset, sib.offset + sib.node.nodeSize, {
                    style: 'display: none;',
                }));
            }
        }
        return DecorationSet.create(doc, decos);
    };

    return new Plugin({
        key,
        state: {
            init: (_config, state) => build(state),
            // Rebuild on doc changes, explicit fold toggles, AND selection moves
            // (so the caret entering/leaving a heading flips colmd-active).
            apply: (tr, value, _old, newState) => {
                if (tr.docChanged || tr.getMeta(key) || tr.selectionSet) return build(newState);
                return value.map(tr.mapping, tr.doc);
            },
        },
        props: {
            decorations(state) { return key.getState(state); },
        },
    });
}

// Publish a serialized snapshot on each document transaction. Selection and
// host replacement transactions never become edits.
export function documentSnapshotView(publish: (doc: any) => void, external: () => boolean) {
    return {
        update: (view, previous) => {
            if (view.state.doc !== previous.doc && !external()) publish(view.state.doc);
        },
    };
}

// ---------------------------------------------------------------------------
// §3b Cell-file links + images. Markdown in a cell may reference its
// NEIGHBORS — `[data](results.csv)`, `![plot](charts/plot.png)` — always
// relative to the md file itself and always within the SAME cell (there is
// deliberately no cross-cell reach). resolveCellPath (embeds.ts) maps such an
// href to the file's full cell-relative path (the getSiblingFile/getFileUrl
// addressing); anything external (scheme, protocol-relative, pure anchor)
// or escaping the cell root resolves to null and is left alone.
// ---------------------------------------------------------------------------
export { resolveCellPath } from './embeds';

/** Follow a click on a link in the editor column (its text, or the address
 *  in Crepe's link tooltip, which mounts inside the column): a link to a
 *  file of the cell opens that file in a NEW tab under its own renderer
 *  (`reveal(cellPath, { newTab: true })`, the host's api.revealFile), so the
 *  document's own tab stays. The tooltip's address follows on a plain click;
 *  a link in the text follows on Cmd/Ctrl-click while editable (a plain
 *  click places the caret) and on a plain click while read-only. A followed
 *  click never reaches the browser: the tooltip's `target=_blank` would be
 *  a popup the sandbox blocks, and a bare link would navigate the frame.
 *  Read-only, every link's default is prevented (with contenteditable off a
 *  bare click would navigate the frame away). An external URL is never
 *  opened: that would take a capability the renderer api does not offer.
 *  True when the click opened a file. */
export function followLinkClick(e, mdPath, readOnly, reveal) {
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a) return false;
    if (readOnly) e.preventDefault();
    const cellPath = resolveCellPath(mdPath, a.getAttribute('href') || '');
    if (!cellPath) return false;
    const tooltipAddress = !!a.closest('.milkdown-link-preview');
    if (!tooltipAddress && !readOnly && !e.metaKey && !e.ctrlKey) return false;
    e.preventDefault();
    e.stopPropagation();
    reveal(cellPath, { newTab: true });
    return true;
}

// Insert (the slash menu's "Insert file…") and a drop from the Files pane are
// the ways a file enters the document. Insert lists the cell's files (the
// accepted paths from api.getFilePaths()) under the caret and writes a
// reference to the chosen one there; a drag of this cell's files from the
// Files pane writes the same reference for each at the drop point
// (dropCellFiles). An image is written as `![name](path)`, anything else as
// `[name](path)`, which the machinery above then displays and follows.
//
// There are no uploads. The editor runs in an iframe host, whose api stages
// no bytes (byte drafts are a native-host member; the sibling write takes
// text), so Crepe's stock paths (the slash menu's Image item, an empty
// image's Upload control, pasting or dropping an image file) could only
// write a blob: URL, which dies with the frame. buildCrepe removes each of
// them, and pasted or dropped HTML keeps only the images that are references
// (stripPastedImages): an image enters the document only as a reference —
// through Insert, as markdown typed or pasted as text, or copied from a
// document that already holds it.

/** The href for `cellPath` written in the markdown file at `mdPath`: relative
 *  to the file's own directory (`../` to climb), each segment percent-encoded
 *  — the inverse of resolveCellPath. */
export function relativeHref(mdPath, cellPath) {
    const from = mdPath.split('/').slice(0, -1);
    const to = cellPath.split('/');
    let shared = 0;
    while (shared < from.length && shared < to.length - 1 && from[shared] === to[shared]) shared += 1;
    return [...from.slice(shared).map(() => '..'), ...to.slice(shared).map(encodeURIComponent)].join('/');
}

/** What Insert writes for `cellPath` in the markdown file at `mdPath`. */
export function insertionFor(mdPath, cellPath) {
    return {
        kind: IMAGE_EXT.test(cellPath) ? 'image' : 'link',
        href: relativeHref(mdPath, cellPath),
        label: cellPath.slice(cellPath.lastIndexOf('/') + 1),
    };
}

/** The files Insert offers: the cell's files but the document itself and
 *  the hidden keep marker an empty folder holds (`<folder>/.collkeep`, never
 *  shown in the Files tree), once each, sorted. */
export function insertCandidates(paths, mdPath) {
    return [...new Set(paths)]
        .filter((p) => p && p !== mdPath && p.slice(p.lastIndexOf('/') + 1) !== '.collkeep')
        .sort((a, b) => a.localeCompare(b));
}

/** The Insert list's preferred width, and the margin it keeps from the
 *  editor column's edges. */
export const INSERT_LIST_WIDTH = 300;
export const INSERT_LIST_MARGIN = 8;
/** The gap between the caret and the list beside it. */
export const INSERT_LIST_GAP = 6;
/** Below the caret the list needs at least this much height, or it opens
 *  above the caret when there is more room there. */
export const INSERT_LIST_MIN_HEIGHT = 160;

/** Where the Insert list goes, from the caret's box (`anchor`) and the
 *  editor column's box (`column`, the list's positioned ancestor), both
 *  viewport rects: `left` and `top` relative to the column, `width`,
 *  `maxHeight` (the list's height cap) and `above`. The list hangs below the
 *  caret, its left edge at the caret, but it is never wider than the column
 *  less a margin on each side, and it moves left (or right) until both its
 *  edges are inside the column: in a narrow cell it spans the column. Its
 *  height stops a margin above the column's bottom; when that leaves less
 *  than INSERT_LIST_MIN_HEIGHT and the room above the caret is larger, it
 *  opens above instead (`above`: `top` is then its bottom edge, which the
 *  list lifts itself onto). The file list scrolls inside it. */
export function placeInsertList(anchor, column, width) {
    const w = Math.max(0, Math.min(width, column.right - column.left - 2 * INSERT_LIST_MARGIN));
    const lowest = column.left + INSERT_LIST_MARGIN;
    const highest = column.right - INSERT_LIST_MARGIN - w;
    const left = Math.max(lowest, Math.min(anchor.left, highest));
    const roomBelow = column.bottom - INSERT_LIST_MARGIN - (anchor.bottom + INSERT_LIST_GAP);
    const roomAbove = anchor.top - INSERT_LIST_GAP - (column.top + INSERT_LIST_MARGIN);
    const above = roomBelow < INSERT_LIST_MIN_HEIGHT && roomAbove > roomBelow;
    return {
        left: left - column.left,
        top: (above ? anchor.top - INSERT_LIST_GAP : anchor.bottom + INSERT_LIST_GAP) - column.top,
        width: w,
        maxHeight: Math.max(0, above ? roomAbove : roomBelow),
        above,
    };
}

/** The editor's dragover (editorViewOptions handleDOMEvents): a drag of cell
 *  files from the Files pane may drop on an editable editor, as a copy.
 *  Anything else is ProseMirror's (and the no-uploads wiring's) as before. */
export function allowCellFileDrag(view, event) {
    if (!view.editable || !event.dataTransfer || !isCellFileDrag(event.dataTransfer)) return false;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    return true;
}

/** The editor's handleDrop for a drag of cell files: the files of THIS cell
 *  (readCellFileDrop compares the payload's cell with api.getCellId()) go to
 *  `place(pos, paths)` at the document position under the pointer. Another
 *  cell's files are refused, since a reference is cell-relative, and so is a
 *  drop outside the document's positions; a cell-file drag is never parsed
 *  as text. Returns false only for a drag that is not cell files. */
export function dropCellFiles(view, event, api, place) {
    const dt = event && event.dataTransfer;
    if (!dt || !isCellFileDrag(dt)) return false;
    if (!view.editable) return true;
    const paths = readCellFileDrop(dt, api);
    if (!paths || paths.length === 0) return true;
    const at = view.posAtCoords({ left: event.clientX, top: event.clientY });
    if (!at) return true;
    place(at.pos, paths);
    return true;
}

/** Pasted or dropped HTML without the images that are not references (the
 *  editor's transformPastedHTML), parsed by the browser in an inert template.
 *  HTML a ProseMirror editor copied (this document, another one, a comment)
 *  marks itself with data-pm-slice: it is a document's own content, so copy,
 *  cut and paste keep its images, all but a blob: one (dead with the frame
 *  that made it). From anywhere else (a web page, an office app, a file
 *  manager) an image survives only as a relative path, a file of the cell;
 *  a web, data: or blob: image is removed. */
export function stripPastedImages(html) {
    const template = document.createElement('template');
    template.innerHTML = html;
    const fromEditor = template.content.querySelector('[data-pm-slice]') !== null;
    let removed = false;
    for (const img of Array.from(template.content.querySelectorAll('img'))) {
        const src = (img.getAttribute('src') || '').trim();
        const reference = fromEditor
            ? !/^blob:/i.test(src)
            : src !== '' && !/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(src);
        if (!reference) {
            img.remove();
            removed = true;
        }
    }
    return removed ? template.innerHTML : html;
}

// The slash menu item's icon (Material's attach_file), in Crepe's icon form.
const INSERT_FILE_ICON = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5c0-1.38 1.12-2.5 2.5-2.5s2.5 1.12 2.5 2.5v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z"/></svg>`;

// ---------------------------------------------------------------------------
// §3c Embeds. `![…](…)` previews its target and `[…](…)` stays a link; a
// preview's size and alignment ride in a pandoc-style attribute block right
// after it, `![plot](charts/plot.png){width=60% align=center}`, written only
// when one is set (embed-markdown.ts holds the encoding, shared with the
// snapshot codec; embeds.ts the previews, the layout and the toolbar's
// writes).
//
//   • The schema: commonmark's image and Crepe's image block carry `width`
//     and `align`; the block keeps the real alt (Crepe wrote its height
//     ratio there, `![1.00](…)`) and has no ratio. Crepe's height handle is
//     hidden (EDITOR_CSS) and an image keeps its aspect.
//   • The markdown: embedRemarkPlugin puts the transform that reads the
//     attribute block first among the remark plugins, ahead of Crepe's
//     remark-image-block.
//   • The views: after create, Crepe's image views are wrapped
//     (createEmbeds) so an image previews as Crepe draws it, a video or an
//     audio file plays, and any other file of the cell is a card whose Open
//     shows it in a new tab of the cell; a path the host refuses is a
//     missing-file card, and an embed re-resolves when its file changes.
//   • The toolbar (EmbedToolbar, §4): over a selected embed, width presets
//     and a size handle, Left / Center / Right, and Link; over a link to an
//     image, a video or an audio file of the cell, Preview. Never while
//     read-only.
// ---------------------------------------------------------------------------

/** The embeds' remark transform, first among the remark plugins. */
const embedRemarkPlugin = (ctx) => async () => {
    await ctx.wait(InitReady);
    const plugin = { plugin: remarkEmbedAttributes, options: {} };
    ctx.update(remarkPluginsCtx, (list) => [plugin, ...list]);
    return () => ctx.update(remarkPluginsCtx, (list) => list.filter((x) => x !== plugin));
};

/** Reports the view on every update (and null when it goes), for the
 *  toolbar's target (embedToolbarTarget). */
function createEmbedToolbarPlugin(report) {
    return new Plugin({
        key: new PluginKey('colmd-embed-toolbar'),
        view: (view) => {
            report(view);
            return { update: (next) => report(next), destroy: () => report(null) };
        },
    });
}

// ---------------------------------------------------------------------------
// §4 UI atoms. Inline styles only (iframe, no Tailwind), on the skin tokens
// the host mirrors onto :root, so the chrome follows a skin change with the
// editor. The style control (theme, font, text size) is the SDK's
// StyleControl, mounted by the editor.
// ---------------------------------------------------------------------------

const surfaceStyle = {
    background: 'var(--sheet-2)',
    color: 'var(--ink)',
    border: '1px solid var(--line-2)',
};

// Insert (§3b): the file list the slash menu's "Insert file…" opens, under
// the caret. The list is the cell's files but the document, read from
// api.getFilePaths() as it opens; typing filters it, the arrows and Enter
// pick, Escape or a press outside closes. Mounted only while open and the
// editor is editable. It stays inside the editor column (`columnRef`, its
// positioned ancestor; placeInsertList), at the caret (`getAnchor`, the
// caret's viewport box), measured as it opens and whenever the column
// scrolls or resizes.
function InsertList({ api, filename, onPick, onClose, columnRef, getAnchor }) {
    const [paths, setPaths] = useState(null);
    const [error, setError] = useState(null);
    const [query, setQuery] = useState('');
    const [active, setActive] = useState(0);
    const [place, setPlace] = useState(null);
    const boxRef = useRef(null);
    const inputRef = useRef(null);

    // Measured before paint, so the list never shows where it would not fit.
    // Without a caret box (no editor yet) it hangs from the column's top
    // right.
    useLayoutEffect(() => {
        const column = columnRef.current;
        const measure = () => {
            if (!column) return;
            const r = column.getBoundingClientRect();
            const caret = getAnchor();
            const anchor = caret || {
                left: r.right, right: r.right, top: r.top + INSERT_LIST_MARGIN, bottom: r.top + INSERT_LIST_MARGIN,
            };
            setPlace(placeInsertList(anchor,
                { left: r.left, right: r.right, top: r.top, bottom: r.bottom }, INSERT_LIST_WIDTH));
        };
        measure();
        const observer = column && typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
        if (observer) observer.observe(column);
        window.addEventListener('resize', measure);
        // The editor column scrolls inside the box; scroll does not bubble.
        if (column) column.addEventListener('scroll', measure, true);
        return () => {
            if (observer) observer.disconnect();
            window.removeEventListener('resize', measure);
            if (column) column.removeEventListener('scroll', measure, true);
        };
    }, []);

    useEffect(() => {
        let live = true;
        api.getFilePaths().then(
            (all) => { if (live) setPaths(insertCandidates(all, filename)); },
            (e) => { if (live) setError(String((e && e.message) || e)); },
        );
        const onDown = (e) => {
            if (boxRef.current && !boxRef.current.contains(e.target)) onClose();
        };
        document.addEventListener('mousedown', onDown, true);
        if (inputRef.current) inputRef.current.focus();
        return () => {
            live = false;
            document.removeEventListener('mousedown', onDown, true);
        };
    }, []);

    const needle = query.trim().toLowerCase();
    const shown = (paths || []).filter((p) => p.toLowerCase().includes(needle));
    const onKeyDown = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); onPick(null); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); setActive((i) => Math.min(i + 1, Math.max(shown.length - 1, 0))); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
        else if (e.key === 'Enter' && shown[active]) { e.preventDefault(); onPick(shown[active]); }
    };
    const note = (text) => (
        <div style={{ padding: '0.5rem 0.625rem', fontSize: '0.78125rem', color: 'var(--ink-3)' }}>{text}</div>
    );

    return (
        <div
            ref={boxRef}
            role="dialog"
            aria-label="Insert a file of this cell"
            style={{
                position: 'absolute',
                zIndex: 20,
                // Placed before the first paint (the layout effect above).
                ...(place
                    ? { left: place.left, top: place.top, width: place.width, maxHeight: place.maxHeight,
                        transform: place.above ? 'translateY(-100%)' : 'none' }
                    : { left: 0, top: 0, width: INSERT_LIST_WIDTH, visibility: 'hidden' }),
                display: 'flex',
                flexDirection: 'column',
                padding: '0.25rem',
                boxSizing: 'border-box',
                borderRadius: 'var(--radius)',
                ...surfaceStyle,
                boxShadow: 'var(--shadow)',
                fontFamily: 'var(--font-ui)',
            }}
        >
            <input
                ref={inputRef}
                value={query}
                placeholder="Find a file"
                aria-label="Find a file"
                onChange={(e) => { setQuery(e.target.value); setActive(0); }}
                onKeyDown={onKeyDown}
                style={{
                    flex: 'none',
                    width: '100%',
                    boxSizing: 'border-box',
                    margin: '0 0 0.25rem',
                    padding: '0.375rem 0.5rem',
                    border: 'none',
                    outline: '1px solid var(--line-2)',
                    borderRadius: '0.375rem',
                    background: 'var(--sheet-3)',
                    color: 'var(--ink)',
                    caretColor: 'var(--teal)',
                    fontFamily: 'var(--font-ui)',
                    fontSize: '0.8125rem',
                }}
            />
            <div role="listbox" style={{ flex: '0 1 auto', minHeight: 0, maxHeight: '17.5rem', overflowY: 'auto' }}>
                {error ? note(`Could not list this cell's files: ${error}`)
                    : paths == null ? note('Loading…')
                    : paths.length === 0 ? note('No other files in this cell')
                    : shown.length === 0 ? note('No file matches')
                    : shown.map((path, i) => (
                        <div
                            key={path}
                            role="option"
                            aria-selected={i === active}
                            title={path}
                            onMouseEnter={() => setActive(i)}
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => onPick(path)}
                            style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '0.625rem',
                                height: '1.875rem',
                                padding: '0 0.625rem',
                                borderRadius: '0.375rem',
                                cursor: 'pointer',
                                fontSize: '0.8125rem',
                                color: i === active ? 'var(--ink)' : 'var(--ink-2)',
                                background: i === active ? 'var(--brass-soft)' : 'transparent',
                            }}
                        >
                            <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {path}
                            </span>
                            <span style={{ flex: 'none', fontSize: '0.6875rem', color: 'var(--ink-3)' }}>
                                {insertionFor(filename, path).kind}
                            </span>
                        </div>
                    ))}
            </div>
        </div>
    );
}

// The embed toolbar (§3c): over a selected embed, its width (presets, and a
// handle on the edge that moves, which previews live and writes on release),
// its alignment and Link; over a link to a file that previews, Preview. It
// never covers the lines around its target (placeEmbedToolbar): on an
// image or a video, inside its box along the top edge; for a link, an
// audio player or a card, below it, above only without room below. The size handle
// keeps clear of it (placeEmbedHandle). Both are measured on mount,
// whenever the target changes, and as the column scrolls or the target
// resizes. A press on it never takes the editor's focus or
// selection. Mounted only while the editor is editable.
const ALIGN_ICONS = {
    left: 'M3 21h18v-2H3v2zm0-4h12v-2H3v2zm0-4h18v-2H3v2zm0-4h12V7H3v2zm0-6v2h18V3H3z',
    center: 'M7 15v2h10v-2H7zm-4 6h18v-2H3v2zm0-8h18v-2H3v2zm4-6v2h10V7H7zM3 3v2h18V3H3z',
    right: 'M3 21h18v-2H3v2zm6-4h12v-2H9v2zm-6-4h18v-2H3v2zm6-4h12V7H9v2zM3 3v2h18V3H3z',
};

function ToolbarButton({ label, pressed, onPress, data, children }) {
    return (
        <button
            type="button"
            aria-label={label}
            title={label}
            aria-pressed={pressed === undefined ? undefined : pressed}
            {...data}
            onMouseDown={(e) => e.preventDefault()}
            onClick={onPress}
            style={{
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '1.5rem',
                minWidth: '1.5rem',
                padding: '0 0.375rem',
                border: 'none',
                borderRadius: '0.3125rem',
                background: pressed ? 'var(--brass-soft)' : 'transparent',
                color: pressed ? 'var(--ink)' : 'var(--ink-2)',
                fontFamily: 'var(--font-ui)',
                fontSize: '0.75rem',
                fontWeight: 500,
                cursor: 'pointer',
            }}
        >
            {children}
        </button>
    );
}

function ToolbarDivider() {
    return <span aria-hidden="true" style={{ width: 1, alignSelf: 'stretch', margin: '0.1875rem', background: 'var(--line-2)' }} />;
}

function EmbedToolbar({ target, viewRef, columnRef, filename, fileUrl }) {
    const boxRef = useRef(null);
    const dragRef = useRef(null);
    const [place, setPlace] = useState(null);
    const [handle, setHandle] = useState(null);
    const key = embedTargetKey(target);
    const embed = target.type === 'embed';
    const attrs = embed ? target.attrs : null;
    const kind = embed ? resolveEmbed(attrs.src, filename, fileUrl).kind : null;
    const sizable = embed && embedSizable(kind);
    const align = embed ? attrs.align : null;

    // The embed's node view (its wrapper; the box is its first child).
    const embedDom = () => {
        const view = viewRef.current;
        const dom = view && embed ? view.nodeDOM(target.pos) : null;
        return dom && dom.nodeType === 1 ? dom : null;
    };
    const anchorBox = () => {
        const view = viewRef.current;
        if (!view) return null;
        if (embed) {
            const dom = embedDom();
            const el = dom && (dom.firstElementChild || dom);
            return el ? el.getBoundingClientRect() : null;
        }
        try {
            const a = view.coordsAtPos(target.from);
            const b = view.coordsAtPos(target.to);
            return { left: Math.min(a.left, b.left), right: Math.max(a.right, b.right),
                top: Math.min(a.top, b.top), bottom: Math.max(a.bottom, b.bottom) };
        } catch { return null; }
    };

    useLayoutEffect(() => {
        const column = columnRef.current;
        const measure = () => {
            const anchor = anchorBox();
            const box = boxRef.current;
            if (!column || !anchor || !box) { setPlace(null); setHandle(null); return; }
            const r = column.getBoundingClientRect();
            const bounds = { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
            const size = { width: box.offsetWidth, height: box.offsetHeight };
            const next = placeEmbedToolbar(anchor, bounds, size, { host: embed && embedHostsToolbar(kind), align });
            const left = next.left + r.left;
            const top = next.top + r.top;
            setPlace(next);
            setHandle(sizable
                ? placeEmbedHandle(anchor, bounds, align, { left, top, right: left + size.width, bottom: top + size.height })
                : null);
        };
        measure();
        const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
        const dom = embedDom();
        if (observer && column) observer.observe(column);
        if (observer && dom) observer.observe(dom.firstElementChild || dom);
        window.addEventListener('resize', measure);
        if (column) column.addEventListener('scroll', measure, true);
        return () => {
            if (observer) observer.disconnect();
            window.removeEventListener('resize', measure);
            if (column) column.removeEventListener('scroll', measure, true);
        };
    }, [key]);

    const run = (make) => {
        const view = viewRef.current;
        if (!view || !view.editable) return;
        view.dispatch(make(view.state));
        view.focus();
    };
    const setWidth = (width) => run((state) => setEmbedLayout(state, target.pos, { width }));
    const setAlign = (value) => run((state) => setEmbedLayout(state, target.pos, { align: value }));

    // The size handle: the box and its column measured as the drag begins,
    // the width previewed on the node view's DOM, one write on release.
    const onHandleDown = (e) => {
        const dom = embedDom();
        if (!dom || e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        const inline = dom.getAttribute('data-embed-inline') === 'true';
        const column = inline && dom.parentElement ? dom.parentElement : dom;
        dragRef.current = {
            dom,
            box: (dom.firstElementChild || dom).getBoundingClientRect(),
            column: column.getBoundingClientRect(),
            width: null,
        };
        e.currentTarget.setPointerCapture(e.pointerId);
    };
    const onHandleMove = (e) => {
        const drag = dragRef.current;
        if (!drag) return;
        const width = draggedWidth(align, drag.box, drag.column, e.clientX);
        if (width == null) return;
        drag.width = width;
        applyEmbedLayout(drag.dom, { width, align });
    };
    const onHandleUp = () => {
        const drag = dragRef.current;
        dragRef.current = null;
        if (drag && drag.width != null && drag.width !== attrs.width) setWidth(drag.width);
    };

    return (
        <>
            <div
                ref={boxRef}
                role="toolbar"
                aria-label={embed ? 'Embed' : 'Link'}
                data-embed-toolbar={target.type}
                onMouseDown={(e) => e.preventDefault()}
                style={{
                    position: 'absolute',
                    zIndex: 15,
                    ...(place ? { left: place.left, top: place.top } : { left: 0, top: 0, visibility: 'hidden' }),
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.0625rem',
                    padding: '0.1875rem',
                    borderRadius: 'var(--radius)',
                    ...surfaceStyle,
                    boxShadow: 'var(--shadow)',
                    fontFamily: 'var(--font-ui)',
                    whiteSpace: 'nowrap',
                }}
            >
                {sizable && EMBED_WIDTH_PRESETS.map((w) => (
                    <ToolbarButton key={w} label={`Width ${w}%`} pressed={attrs.width === w}
                        data={{ 'data-embed-width': String(w) }}
                        onPress={() => setWidth(attrs.width === w ? null : w)}>
                        {w}%
                    </ToolbarButton>
                ))}
                {sizable && <ToolbarDivider />}
                {sizable && ['left', 'center', 'right'].map((value) => (
                    <ToolbarButton key={value} label={`Align ${value}`}
                        pressed={value === 'left' ? !align || align === 'left' : align === value}
                        data={{ 'data-embed-align': value }}
                        onPress={() => setAlign(value === 'left' || align === value ? null : value)}>
                        <svg viewBox="0 0 24 24" style={{ width: '1rem', height: '1rem' }} fill="currentColor" aria-hidden="true">
                            <path d={ALIGN_ICONS[value]} />
                        </svg>
                    </ToolbarButton>
                ))}
                {sizable && <ToolbarDivider />}
                {embed ? (
                    <ToolbarButton label="Show as a link" data={{ 'data-embed-action': 'link' }}
                        onPress={() => run((state) => embedToLink(state, target.pos, TextSelection))}>
                        Link
                    </ToolbarButton>
                ) : (
                    <ToolbarButton label="Show as a preview" data={{ 'data-embed-action': 'preview' }}
                        onPress={() => run((state) => linkToEmbed(state, target, NodeSelection))}>
                        Preview
                    </ToolbarButton>
                )}
            </div>
            {handle && (
                <div
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Drag to resize"
                    aria-valuenow={attrs.width == null ? undefined : attrs.width}
                    data-embed-handle="width"
                    onPointerDown={onHandleDown}
                    onPointerMove={onHandleMove}
                    onPointerUp={onHandleUp}
                    onPointerCancel={onHandleUp}
                    style={{
                        position: 'absolute',
                        zIndex: 15,
                        left: handle.left,
                        top: handle.top,
                        width: EMBED_HANDLE_WIDTH,
                        height: handle.height,
                        boxSizing: 'border-box',
                        borderRadius: '0.25rem',
                        border: '1px solid var(--sheet)',
                        background: 'var(--teal)',
                        cursor: 'ew-resize',
                        touchAction: 'none',
                    }}
                />
            )}
        </>
    );
}

// ---------------------------------------------------------------------------
// §5 The editor component.
// ---------------------------------------------------------------------------

// An external body applied in place as ONE transaction over the changed range
// only (the documents' common prefix and suffix stay untouched nodes): the
// selection maps through the step instead of jumping to the document start,
// and the replacement is kept out of undo history (addToHistory false), which
// plugin-listener also skips, so it never comes back as a user edit. The
// caller keeps the scroll column's offset.
function replaceChangedRange(markdown) {
    return (ctx) => {
        const view = ctx.get(editorViewCtx);
        const next = ctx.get(parserCtx)(markdown);
        if (!next) return;
        const { state } = view;
        const start = state.doc.content.findDiffStart(next.content);
        if (start == null) return;
        let { a: endA, b: endB } = state.doc.content.findDiffEnd(next.content);
        const overlap = start - Math.min(endA, endB);
        if (overlap > 0) { endA += overlap; endB += overlap; }
        const tr = state.tr.replace(start, endA, next.slice(start, endB));
        tr.setMeta('addToHistory', false);
        view.dispatch(tr);
    };
}

// Caret at the end: a press on the editor column below the last block puts
// the caret at the end of the document, as a click below a page's text does
// in a word processor. The last block's bottom is that of the last child of
// .ProseMirror that takes up space (a folded block or a widget does not).
export function belowLastBlock(pm, clientY) {
    for (let el = pm.lastElementChild; el; el = el.previousElementSibling) {
        const r = el.getBoundingClientRect();
        if (r.height > 0 || r.width > 0) return clientY > r.bottom;
    }
    return false;
}

/** The caret at the document's end, focused. When the last block is not a
 *  textblock (a code block, a table, an image, a rule) an empty paragraph
 *  is appended first, so there is somewhere to type. */
export function placeCaretAtEnd(view) {
    const { state } = view;
    let tr = state.tr;
    const last = state.doc.lastChild;
    const paragraph = state.schema.nodes.paragraph;
    if ((!last || !last.isTextblock) && paragraph) tr = tr.insert(state.doc.content.size, paragraph.create());
    view.dispatch(tr.setSelection(TextSelection.atEnd(tr.doc)).scrollIntoView());
    view.focus();
}

function MilkdownEditor() {
    const { content, isReadOnly, api } = useCollimateFile();
    const { onChange: onThemeChange, scheme, known } = useTheme(api);
    // The style (§2): theme, font and text size, the host's record for this
    // renderer, which StyleControl below writes.
    const style = useStyle(api);
    const filename = api.getFilename();
    const containerRef = useRef(null);
    const editorColRef = useRef(null);
    const crepeRef = useRef(null);
    // Keep host bytes distinct from the editor's normalized serialization.
    const lastEmittedRef = useRef(content || '');
    // Raw host bytes differ from Crepe's normalized markdown; compare each
    // representation against its own baseline.
    const lastFileRef = useRef(content || '');
    const lastSnapshotIdRef = useRef(null);
    const lastSnapshotDocRef = useRef(null);
    const snapshotOriginRef = useRef(null);
    if (!snapshotOriginRef.current) snapshotOriginRef.current = crypto.randomUUID();
    const snapshotCounterRef = useRef(0);
    // Folded-heading keys retained by the live editor instance.
    const foldStateRef = useRef(new Set());
    // Surface editor creation or external-content parser failures in the cell.
    const [loadError, setLoadError] = useState(null);
    // Insert's file list (§3b), opened by the slash menu's "Insert file…".
    const [insertOpen, setInsertOpen] = useState(false);
    // The embed toolbar's target (§3c) and the view it acts on, reported by
    // its plugin on every editor update; the key keeps an unchanged target
    // from re-rendering.
    const [embedTarget, setEmbedTarget] = useState(null);
    const embedViewRef = useRef(null);
    const embedKeyRef = useRef('');
    const reportEmbed = (view) => {
        embedViewRef.current = view;
        const target = view ? embedToolbarTarget(view.state, view.editable, filename) : null;
        const key = embedTargetKey(target);
        if (key === embedKeyRef.current) return;
        embedKeyRef.current = key;
        setEmbedTarget(target);
    };
    // A file of this cell as the host serves it, or null when the host
    // refuses the path (not a file of the cell): never throws, so a stale
    // reference shows a missing-file card instead of breaking the view.
    const fileUrl = (cellPath) => {
        try { return api.getFileUrl(cellPath); } catch { return null; }
    };

    // Text size. Applied as a CSS variable on the document root that a
    // `zoom: var(--colmd-zoom)` rule on .ProseMirror reads — so it survives
    // content updates without re-touching the editor, and scales every
    // content element (the prose rules size headings, code and table heads in
    // px, which a plain font-size would not reach) while leaving the chrome
    // native.
    useEffect(() => {
        document.documentElement.style.setProperty('--colmd-zoom', String(textSizeZoom(style.textSize)));
    }, [style.textSize]);

    // Font: the chosen family's stack on --colmd-font, which the prose reads
    // before the skin's --font-prose; none for the skin's face.
    useEffect(() => {
        const stack = fontStack(style.font, api.getFontFamilies ? api.getFontFamilies() : []);
        const root = document.documentElement;
        if (stack) root.style.setProperty('--colmd-font', stack);
        else root.style.removeProperty('--colmd-font');
    }, [style.font]);

    // Theme: a library family's sheet for the skin's scheme, once the host's
    // record is known (the placeholder's scheme is a guess); the skin's map
    // alone otherwise.
    useEffect(() => {
        const href = known ? libraryThemeUrl(style.theme, scheme) : null;
        if (!href) {
            setLibraryThemeSheet(null);
            return undefined;
        }
        let live = true;
        libraryThemeCss(href).then(
            (css) => { if (live) setLibraryThemeSheet(css); },
            (e) => {
                console.warn('milkdown: library theme failed to load', href, e);
                if (live) setLibraryThemeSheet(null);
            },
        );
        return () => { live = false; };
    }, [style.theme, scheme, known]);

    // Insert (§3b): a reference to `cellPath` at the selection, written with
    // `ctx`'s schema. An image is commonmark's inline image node, so the
    // markdown reads `![name](path)`; on the next load a lone image becomes
    // Crepe's image block, which keeps the name as its alt (§3c). Anything
    // else is its name, linked. Shared by the Insert list and a drop.
    const insertAt = (view, ctx, cellPath) => {
        const { kind, href, label } = insertionFor(filename, cellPath);
        const { state } = view;
        let tr;
        if (kind === 'image') {
            tr = state.tr.replaceSelectionWith(imageSchema.type(ctx).create({ src: href, alt: label }));
        } else {
            const link = linkSchema.type(ctx);
            tr = state.tr.replaceSelectionWith(state.schema.text(label, [link.create({ href })]), false)
                .removeStoredMark(link);
        }
        view.dispatch(tr.scrollIntoView());
    };

    // A drop of this cell's files (dropCellFiles): the selection moves to the
    // drop point and each file is inserted there, a space between two, the
    // document itself left out.
    const dropAt = (view, ctx, pos, paths) => {
        const { state } = view;
        view.dispatch(state.tr.setSelection(TextSelection.near(state.doc.resolve(pos))));
        paths.filter((p) => p && p !== filename).forEach((cellPath, i) => {
            if (i > 0) view.dispatch(view.state.tr.insertText(' '));
            insertAt(view, ctx, cellPath);
        });
        view.focus();
    };

    useEffect(() => {
        if (!containerRef.current) return;
        let cancelled = false;
        let unsubDocument = null;
        let codecReady = false;
        let latestHostSequence = -1;
        let lastHostRecoveryId = null;
        let unsubFlush = null;
        // Host replacements are applied without emitting another local edit.
        let applyingExternal = false;
        let recoveryPromise = null;
        let pendingRecoveryFresh = null;
        // The embeds' node views (§3c), for every editor this mount builds.
        const embeds = createEmbeds({
            mdPath: filename,
            fileUrl,
            reveal: (cellPath, options) => api.revealFile(cellPath, options),
            NodeSelection,
        });

        // ------------------------------------------------------------------
        // Editor construction.
        // ------------------------------------------------------------------

        // Apply only bodies chosen by the document owner, preserving selection
        // and undo history through one ranged transaction.
        const replaceContent = async (fresh) => {
            if (cancelled || fresh == null) return;
            if (!crepeRef.current) {
                // Only initial creation failure recovers by creating an instance.
                // One creation runs at a time; newer writes replace its pending
                // content, then apply through the same live-instance path.
                pendingRecoveryFresh = fresh;
                if (!recoveryPromise) {
                    recoveryPromise = (async () => {
                        // A failed attempt retries only for a newer queued write,
                        // never in an automatic loop over the same failing source.
                        while (!cancelled && !crepeRef.current) {
                            const initial = pendingRecoveryFresh;
                            applyingExternal = true;
                            try {
                                const recovered = await createWithFallback(initial);
                                if (cancelled) { recovered.destroy(); return; }
                                crepeRef.current = recovered;
                                lastFileRef.current = initial;
                                lastEmittedRef.current = recovered.getMarkdown();
                                setLoadError(null);
                            } catch (e) {
                                if (!cancelled) setLoadError(String((e && e.message) || e));
                            } finally {
                                applyingExternal = false;
                            }
                            if (pendingRecoveryFresh === initial) break;
                        }
                        const latest = pendingRecoveryFresh;
                        if (!cancelled && crepeRef.current) await replaceContent(latest);
                    })().finally(() => { recoveryPromise = null; });
                }
                await recoveryPromise;
                return;
            }
            if (fresh === lastFileRef.current || fresh === lastEmittedRef.current) {
                lastFileRef.current = fresh;
                return;
            }
            const crepe = crepeRef.current;
            const scrollTop = editorColRef.current?.scrollTop;
            applyingExternal = true;
            try {
                crepe.editor.action(replaceChangedRange(fresh));
                lastEmittedRef.current = crepe.getMarkdown();
                lastFileRef.current = fresh;
                if (editorColRef.current && scrollTop != null) editorColRef.current.scrollTop = scrollTop;
                setLoadError(null);
            } catch (e) {
                console.error('milkdown: editor failed to apply external content', e);
                if (!cancelled) setLoadError(String((e && e.message) || e));
            } finally {
                applyingExternal = false;
            }
        };

        const publishSnapshot = (doc) => {
            if (doc === lastSnapshotDocRef.current) return;
            const snapshot = {
                codecHash: milkdownSnapshotCodec.implementationHash,
                snapshotId: `${snapshotOriginRef.current}:${++snapshotCounterRef.current}`,
                payload: JSON.stringify(doc.toJSON()),
            };
            lastSnapshotDocRef.current = doc;
            lastSnapshotIdRef.current = snapshot.snapshotId;
            // These old strings no longer describe the current document.
            lastFileRef.current = null;
            lastEmittedRef.current = null;
            api.saveSnapshot(snapshot);
        };

        const applyDocument = async (document) => {
            if (cancelled) return;
            if (!codecReady) throw new Error('Snapshot storage is not ready');
            if (document.editSeq < latestHostSequence) return;
            latestHostSequence = document.editSeq;
            if (document.recovery) {
                const recovery = document.recovery;
                if (recovery.codecHash !== milkdownSnapshotCodec.implementationHash) {
                    throw new Error('This document needs a different snapshot codec');
                }
                const repeated = recovery.snapshotId === lastHostRecoveryId;
                lastHostRecoveryId = recovery.snapshotId;
                // Host receipts may trail several local transactions. Every
                // snapshot from this live view is an echo, including older IDs.
                if (repeated || recovery.snapshotId.startsWith(`${snapshotOriginRef.current}:`)) return;
                const crepe = crepeRef.current;
                if (!crepe) throw new Error('Editor is not ready to restore this document');
                applyingExternal = true;
                try {
                    crepe.editor.action(ctx => {
                        const view = ctx.get(editorViewCtx);
                        const next = restoreProseMirrorSnapshot(view.state.schema, recovery.payload);
                        if (!view.state.doc.eq(next)) {
                            view.dispatch(view.state.tr.replaceWith(0, view.state.doc.content.size, next.content)
                                .setMeta('addToHistory', false));
                        }
                        lastSnapshotDocRef.current = view.state.doc;
                    });
                    lastSnapshotIdRef.current = recovery.snapshotId;
                    lastFileRef.current = null;
                    lastEmittedRef.current = null;
                } finally { applyingExternal = false; }
            } else {
                lastHostRecoveryId = null;
                await replaceContent(document.content);
                lastSnapshotIdRef.current = null;
                const crepe = crepeRef.current;
                if (crepe) crepe.editor.action(ctx => { lastSnapshotDocRef.current = ctx.get(editorViewCtx).state.doc; });
            }
            crepeRef.current?.setReadonly(isReadOnly);
        };

        const buildCrepe = (initial) => {
            const featureConfigs = {};
            // Fenced code (§2b): no One Dark, and the skin's highlight.
            featureConfigs[(Crepe.Feature && Crepe.Feature.CodeMirror) || 'code-mirror'] = {
                theme: null,
                extensions: [syntaxHighlighting(skinHighlightStyle())],
            };
            // Images of NEIGHBORING cell files: the markdown stores a clean
            // relative path (`![plot](charts/plot.png)`), and Crepe's image
            // views ask proxyDomURL for the URL to actually render — so the
            // document stays portable while the <img> loads through the
            // cell-file endpoint (accepted-current-revision, binary-safe). External URLs
            // pass through untouched, and a path the host refuses loads
            // nothing (the embed shows a missing-file card, §3c) instead of
            // throwing inside Crepe's view. No uploads (§3b): an upload has nowhere
            // to go, and an empty image's link field asks for a URL or a path
            // in this cell.
            featureConfigs[(Crepe.Feature && Crepe.Feature.ImageBlock) || 'image-block'] = {
                proxyDomURL: (url) => proxiedEmbedUrl(url, filename, fileUrl),
                onUpload: () => Promise.reject(new Error('milkdown: no uploads; Insert places a file of this cell')),
                blockUploadPlaceholderText: 'Paste a link or a path in this cell',
                inlineUploadPlaceholderText: 'Link or path',
            };
            // The slash menu: Crepe's Image item (an empty image with Upload)
            // is gone, and "Insert file…" opens Insert's file list, the typed
            // "/…" cleared first as Crepe's own items do.
            featureConfigs[(Crepe.Feature && Crepe.Feature.BlockEdit) || 'block-edit'] = {
                advancedGroup: { image: null },
                buildMenu: (builder) => {
                    builder.getGroup('advanced').addItem('insert-file', {
                        label: 'Insert file…',
                        icon: INSERT_FILE_ICON,
                        onRun: (ctx) => {
                            ctx.get(commandsCtx).call(clearTextInCurrentBlockCommand.key);
                            setInsertOpen(true);
                        },
                    });
                },
            };
            const c = new Crepe({
                root: editorColRef.current,
                defaultValue: initial || '',
                // Drop the "Please enter…" block placeholder that trails the
                // caret on empty lines. Feature value is the stable enum
                // string 'placeholder' (fall back to it if the static enum
                // isn't on this build of Crepe).
                features: {
                    [(Crepe.Feature && Crepe.Feature.Placeholder) || 'placeholder']: false,
                },
                featureConfigs,
            });
            // No uploads (§3b), after Crepe's builder config (an editor's
            // configs run in the order they were added): a pasted or dropped
            // file inserts nothing, and pasted or dropped HTML keeps only the
            // images that are references.
            c.editor.config((ctx) => {
                ctx.update(uploadConfig.key, (prev) => ({ ...prev, uploader: async () => [] }));
                ctx.update(editorViewOptionsCtx, (prev) => ({ ...prev, transformPastedHTML: stripPastedImages }));
                // A drag of this cell's files from the Files pane drops at
                // the pointer, through Insert's own writing (§3b). The view's
                // own props run before every plugin's, the upload plugin's
                // handleDrop included.
                ctx.update(editorViewOptionsCtx, (prev) => ({
                    ...prev,
                    handleDOMEvents: { ...(prev.handleDOMEvents || {}), dragover: allowCellFileDrag },
                    handleDrop: (view, event) => dropCellFiles(view, event, api,
                        (pos, paths) => dropAt(view, ctx, pos, paths)),
                }));
            });
            // Embeds (§3c): the image nodes with a size and an alignment, and
            // the transform that reads them from the markdown. The document's
            // own format, so never optional: the snapshot codec serializes it.
            c.editor
                .use(imageSchema.extendSchema(extendImageSchema))
                .use(imageBlockSchema.extendSchema(extendImageBlockSchema))
                .use(embedRemarkPlugin);
            // Folding and the embed toolbar can be disabled if their optional
            // plugin wiring fails.
            if (prosePluginsSupported) {
                try {
                    c.editor.use($prose(() => createFoldPlugin(foldStateRef)));
                    c.editor.use($prose(() => createEmbedToolbarPlugin(reportEmbed)));
                } catch { prosePluginsSupported = false; }
            }
            // Keep the view noneditable until the codec is durable and the
            // owner's latest recovery snapshot has been restored.
            c.setReadonly(true);
            // Persistence is required editor wiring. A failed snapshot plugin
            // must fail the mount visibly, never leave an unjournaled editor.
            if (!isReadOnly) c.editor.use($prose(() => new Plugin({
                key: new PluginKey('colmd-document-snapshot'),
                props: { handleDOMEvents: { beforeinput: transactionTextInput(TextSelection) } },
                view: () => documentSnapshotView(publishSnapshot,
                    () => applyingExternal || !crepeRef.current),
            })));
            return c;
        };

        // Build + create a crepe editor, retrying once without our plugins
        // (folding) if create() throws (e.g. a
        // prosemirror instance mismatch). Guarantees the editor mounts even
        // if they can't be wired.
        const createWithFallback = async (initial) => {
            let c = buildCrepe(initial);
            try {
                await c.create();
            } catch (e) {
                try { c.destroy(); } catch {}
                if (!prosePluginsSupported) throw e;
                // Our plugins are the only optional wiring; if they threw at
                // create(), drop them and rebuild a plain crepe editor.
                // buildCrepe reads prosePluginsSupported live, so the retry is plain.
                console.warn('milkdown: our ProseMirror plugins failed to register — retrying without them:', e);
                prosePluginsSupported = false;
                c = buildCrepe(initial);
                await c.create();
            }
            installEmbedViews(c);
            if (isReadOnly) c.setReadonly(true);
            return c;
        };

        // The embeds' node views (§3c) wrap Crepe's own image views, which
        // exist once the editor is created; the view redraws with them. If
        // that fails, the images keep Crepe's views.
        const installEmbedViews = (crepe) => {
            try {
                crepe.editor.action((ctx) => {
                    const view = ctx.get(editorViewCtx);
                    const base = view.props.nodeViews || {};
                    view.setProps({ nodeViews: { ...base, ...embeds.nodeViews(base) } });
                });
            } catch (e) {
                console.warn('milkdown: embed views failed to install', e);
            }
        };

        (async () => {
            // Crepe's own sheets first, then the skin: the token map, the
            // mock's prose and the chrome overrides win at equal specificity.
            api.injectCss(await crepeThemeCss());
            api.injectCss(milkdownCss());
            if (cancelled || !editorColRef.current) return;

            // A create failure must not strand the frame: show the error
            // in-cell, still signal ready, and keep the update subscriptions
            // below so a later write retries the create.
            let crepe = null;
            try {
                await api.registerSnapshotCodec(milkdownSnapshotCodec);
                codecReady = true;
                const initial = await api.getDocumentSnapshot();
                if (cancelled) return;
                crepe = await createWithFallback(initial.recovery ? '' : initial.content);
            } catch (e) {
                console.error('milkdown: editor failed to start', e);
                if (!cancelled) setLoadError(String((e && e.message) || e));
            }
            if (cancelled) { if (crepe) crepe.destroy(); return; }
            if (crepe) {
                crepeRef.current = crepe;
                // Baseline the echo-dedupe on crepe's OWN serialization of the
                // file, not the raw bytes: crepe normalizes (`- [ ]` → `* [ ]`,
                // spacing), so a raw baseline makes the first markdownUpdated
                // after mount look like a user edit of a file nobody touched.
                try { lastEmittedRef.current = crepe.getMarkdown(); }
                catch { lastEmittedRef.current = content || ''; }
            }
            // Document pushes include the current recovery ID. A worker
            // materialization of our own snapshot never reparses the live view.
            unsubDocument = api.onDocumentSnapshot(document => {
                void applyDocument(document).catch(error => setLoadError(String(error?.message || error)));
            });
            if (crepe) {
                try {
                    await applyDocument(await api.getDocumentSnapshot());
                    if (!cancelled) crepe.setReadonly(isReadOnly);
                } catch (error) { setLoadError(String(error?.message || error)); }
            }
            if (api.ready) api.ready();
            // Every transaction is already published. Flush checks only for
            // a genuinely newer PM document; the owner awaits materialization.
            if (api.onFlush) unsubFlush = api.onFlush(() => {
                const current = crepeRef.current;
                if (current && !isReadOnly) current.editor.action(ctx => publishSnapshot(ctx.get(editorViewCtx).state.doc));
            });
        })();

        // Fires at once with the current record, then on every change.
        const unsubTheme = onThemeChange(applyTheme);
        // A changed file of the cell may be a new revision of an embed's
        // target, or a missing one now served (§3c). Only those embeds
        // re-resolve; the document itself never reloads from here.
        const unsubFiles = api.onFilesChange((detail) => embeds.refresh(detail && detail.changedFiles));

        // Editor clicks: a link to a NEIGHBORING cell file opens it in a new
        // tab of the cell (followLinkClick: the tooltip's address on a plain
        // click, a link in the text on Cmd/Ctrl-click, or a plain one while
        // read-only).
        const onEditorClick = (e) => {
            if (!editorColRef.current || !editorColRef.current.contains(e.target)) return;
            followLinkClick(e, filename, isReadOnly, (cellPath, options) => api.revealFile(cellPath, options));
        };
        const colEl = editorColRef.current;
        if (colEl) colEl.addEventListener('click', onEditorClick);

        // Caret at the end: a press on the column below the last block (on
        // the column, .milkdown or .ProseMirror itself, never on a widget
        // hanging there) puts the caret at the document's end. Captured, so
        // ProseMirror's own mousedown never places a caret of its own first.
        const onColumnMouseDown = (e) => {
            const crepe = crepeRef.current;
            if (!crepe || e.button !== 0 || !colEl) return;
            const pm = colEl.querySelector('.ProseMirror');
            const target = e.target;
            const ground = target === colEl || target === pm
                || (target && target.classList && target.classList.contains('milkdown'));
            if (!pm || !ground || !belowLastBlock(pm, e.clientY)) return;
            crepe.editor.action((ctx) => {
                const view = ctx.get(editorViewCtx);
                if (!view.editable) return;
                e.preventDefault();
                e.stopPropagation();
                placeCaretAtEnd(view);
            });
        };
        if (colEl) colEl.addEventListener('mousedown', onColumnMouseDown, true);

        return () => {
            cancelled = true;
            if (unsubDocument) unsubDocument();
            if (unsubFlush) unsubFlush();
            if (unsubTheme) unsubTheme();
            if (unsubFiles) unsubFiles();
            if (colEl) colEl.removeEventListener('click', onEditorClick);
            if (colEl) colEl.removeEventListener('mousedown', onColumnMouseDown, true);
            if (crepeRef.current) crepeRef.current.destroy();
            crepeRef.current = null;
        };
    }, []);

    // Insert (§3b): close the list, write the chosen file at the caret, then
    // give the editor its focus back (null: the list closed on Escape).
    const insertFile = (cellPath) => {
        setInsertOpen(false);
        const crepe = crepeRef.current;
        if (!crepe || isReadOnly) return;
        crepe.editor.action((ctx) => {
            const view = ctx.get(editorViewCtx);
            if (cellPath) insertAt(view, ctx, cellPath);
            view.focus();
        });
    };

    // The Insert list's anchor: the caret's viewport box.
    const caretBox = () => {
        const crepe = crepeRef.current;
        if (!crepe) return null;
        let box = null;
        try {
            crepe.editor.action((ctx) => {
                const view = ctx.get(editorViewCtx);
                const c = view.coordsAtPos(view.state.selection.from);
                box = { left: c.left, right: c.right, top: c.top, bottom: c.bottom };
            });
        } catch { box = null; }
        return box;
    };

    // No inline background on the root: the body rule in milkdownCss paints
    // the page --sheet, and .milkdown inside paints itself via
    // --crepe-color-background (the same token).
    // overflowAnchor: 'none' on the editor column — Crepe inserts/removes
    // widgets (toolbar, link tooltip) on selection; default scroll-anchoring
    // re-anchors when DOM mutates above the viewport, which manifests as
    // "the whole document jumps up by ~24px the moment selection starts."
    // scrollPaddingTop matches the ProseMirror top padding (26px) so that
    // ProseMirror's scrollIntoView doesn't hide that padding behind the top
    // edge.
    return (
        <div ref={containerRef} style={EDITOR_ROOT}>
            <div
                ref={editorColRef}
                style={{ width: '100%', height: '100%', overflowY: 'auto', overflowAnchor: 'none', scrollPaddingTop: '1.625rem' }}
            />

            {loadError && (
                <div role="alert" style={{
                    position: 'absolute',
                    top: '3rem',
                    left: '1rem',
                    right: '1rem',
                    zIndex: 10,
                    padding: '0.625rem 0.875rem',
                    borderRadius: '0.5rem',
                    border: '1px solid var(--red)',
                    background: 'var(--sheet-2)',
                    color: 'var(--ink)',
                    fontFamily: 'var(--font-ui)',
                    fontSize: '0.8125rem',
                    lineHeight: 1.4,
                    overflowWrap: 'anywhere',
                }}>
                    <span style={{ color: 'var(--red)', fontWeight: 600 }}>
                        Could not load the markdown editor:
                    </span>{' '}{loadError}
                </div>
            )}

            {/* Insert's file list, at the caret (never while read-only). */}
            {insertOpen && !isReadOnly && (
                <InsertList
                    api={api}
                    filename={filename}
                    onPick={insertFile}
                    onClose={() => setInsertOpen(false)}
                    columnRef={containerRef}
                    getAnchor={caretBox}
                />
            )}

            {/* The embed toolbar, over a selected embed or a link to a file
                that previews (never while read-only). */}
            {embedTarget && !isReadOnly && (
                <EmbedToolbar
                    key={embedTarget.type}
                    target={embedTarget}
                    viewRef={embedViewRef}
                    columnRef={containerRef}
                    filename={filename}
                    fileUrl={fileUrl}
                />
            )}

            {/* The style control, top right, shown while the pointer is
                near that corner (it watches this editor root, its parent):
                theme, prose font and text size. */}
            <StyleControl
                api={api}
                themes={MILKDOWN_THEMES}
                font="prose"
                size={TEXT_SIZE}
                style={{ top: '0.5rem', right: '0.875rem', zIndex: 20 }}
            />
        </div>
    );
}

export default defineRenderer({ component: MilkdownEditor, deferReady: true, recoverySnapshots: true });
