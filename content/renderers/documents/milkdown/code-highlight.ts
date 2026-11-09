// SPDX-License-Identifier: MIT
// ---------------------------------------------------------------------------
// Fenced code in a Crepe editor, on the skin: the code block's CSS and the
// syntax highlight that draws code in the SDK's syntaxPalette.
//
// The renderer gives Crepe's code-mirror feature `theme: null` (Crepe's
// default is One Dark, a palette of its own) and one extension,
// syntaxHighlighting(HighlightStyle.define(highlightSpecs(tags))). The
// HighlightStyle names no colour: each tag's colour is
// var(--colmd-syntax-<role>), and the renderer writes those properties on
// :root from syntaxVars(syntaxPalette(theme)) on every skin record, so a
// skin change needs no reconfiguration. Before a readable record the
// properties are unset, the colour is invalid at computed-value time, and
// code inherits the block's ink.
// ---------------------------------------------------------------------------
import { SYNTAX_ROLES } from '/api/sdk/react.js';

// The block and CodeMirror's own surfaces, on the tokens. With `theme: null`
// no extension sets EditorView.darkTheme, so CodeMirror applies its light
// base theme (the `&light` rules of @codemirror/view, search and
// autocomplete, scoped `.ͼ2 …`) and its unscoped base rules (`.ͼ1 …`) in
// every skin. Each of those rules that a code block can reach and that sets a
// colour is restated here under `.milkdown .milkdown-code-block`, two classes
// above the base theme's one, so it wins wherever the frame injects it. The
// box is the mock's code block: --code-bg ground, --line edge.
export const CODE_BLOCK_CSS = `
.milkdown .ProseMirror .milkdown-code-block {
    margin: 0.75rem 0 0;
    padding: 0.375rem 0.75rem;
    border: 1px solid var(--line);
    border-radius: 0.375rem;
    background: var(--code-bg);
    color: var(--ink-2);
    font-size-adjust: none;
}
.milkdown .milkdown-code-block .cm-editor,
.milkdown .milkdown-code-block .cm-gutters { background: transparent; }
.milkdown .milkdown-code-block .cm-gutters { color: var(--ink-3); border-right: 0; }
.milkdown .milkdown-code-block .cm-scroller {
    font-family: var(--font-mono);
    font-size: 0.71875rem;
    line-height: 1.55;
}
.milkdown .milkdown-code-block .cm-content { color: var(--ink-2); caret-color: var(--teal); }
.milkdown .milkdown-code-block .cm-cursor,
.milkdown .milkdown-code-block .cm-dropCursor { border-left-color: var(--teal); }
.milkdown .milkdown-code-block .cm-selectionBackground,
.milkdown .milkdown-code-block .cm-content ::selection { background: var(--brass-soft) !important; }
.milkdown .milkdown-code-block .cm-activeLine,
.milkdown .milkdown-code-block .cm-activeLineGutter { background: transparent; }
.milkdown .milkdown-code-block .cm-matchingBracket { background: var(--teal-soft); }
.milkdown .milkdown-code-block .cm-nonmatchingBracket { background: var(--red-soft); }
.milkdown .milkdown-code-block .cm-searchMatch { background: var(--brass-soft); outline: 1px solid var(--brass); }
.milkdown .milkdown-code-block .cm-searchMatch.cm-searchMatch-selected { background: var(--teal-soft); }
.milkdown .milkdown-code-block .cm-selectionMatch { background: var(--teal-soft); }
.milkdown .milkdown-code-block .cm-specialChar { color: var(--red); }
.milkdown .milkdown-code-block .cm-foldPlaceholder { background: var(--sheet-3); border: 1px solid var(--line-2); color: var(--ink-3); }
.milkdown .milkdown-code-block .cm-snippetField { background: var(--teal-soft); }
.milkdown .milkdown-code-block .cm-snippetFieldPosition { border-left-color: var(--ink-3); }

/* Tooltips (the completion list) and panels (search, Mod-f inside a block). */
.milkdown .milkdown-code-block .cm-tooltip,
.milkdown .milkdown-code-block .cm-panels { background: var(--sheet-2); color: var(--ink); border-color: var(--line-2); }
.milkdown .milkdown-code-block .cm-tooltip-section:not(:first-child) { border-top-color: var(--line-2); }
.milkdown .milkdown-code-block .cm-tooltip.cm-tooltip-autocomplete > ul > completion-section { border-bottom-color: var(--line-2); }
.milkdown .milkdown-code-block .cm-tooltip-autocomplete ul li[aria-selected] { background: var(--brass-soft); color: var(--ink); }
.milkdown .milkdown-code-block .cm-tooltip-autocomplete-disabled ul li[aria-selected] { background: var(--sheet-3); color: var(--ink-2); }
.milkdown .milkdown-code-block .cm-panel { font-family: var(--font-ui); }
.milkdown .milkdown-code-block .cm-panel > button,
.milkdown .milkdown-code-block .cm-panel > label input[type='checkbox'] { border-color: var(--line-2); }
.milkdown .milkdown-code-block .cm-button { background-image: none; border-color: var(--line-2); }
.milkdown .milkdown-code-block .cm-textfield { border: 1px solid var(--line-2); background: var(--sheet-3); color: var(--ink); }
.milkdown .milkdown-code-block .cm-textfield:focus { outline: 1px solid var(--teal); }
`;

/** One Lezer highlight tag (a `tags` member, or a modifier applied to one:
 *  'function(variableName)') and the syntax palette role it is drawn in,
 *  with any font style the markup tags need beyond colour. */
export type TagRole = {
    tag: string;
    role: string;
    fontStyle?: string;
    fontWeight?: string;
    textDecoration?: string;
};

/** Every tag the fenced-code highlighter styles. Lezer tags form a
 *  hierarchy and a style for a tag reaches its children (lineComment and
 *  docComment are comments, integer is a number, controlKeyword a keyword,
 *  bracket and separator punctuation), and a modified tag falls back to its
 *  base ('definition(variableName)' is a variableName); a child is listed
 *  where it takes a different role from its parent (tagName is a child of
 *  typeName, attributeName of propertyName). The roles follow the Monaco
 *  renderer's token rules, so both editors draw a language alike. */
export const LEZER_TAG_ROLES: readonly TagRole[] = Object.freeze([
    { tag: 'comment', role: 'comment' },
    { tag: 'meta', role: 'keyword' },
    { tag: 'annotation', role: 'attribute' },
    { tag: 'keyword', role: 'keyword' },
    { tag: 'self', role: 'function' },                       // this, self
    { tag: 'literal', role: 'number' },
    { tag: 'string', role: 'string' },
    { tag: 'special(string)', role: 'regexp' },
    { tag: 'escape', role: 'regexp' },
    { tag: 'regexp', role: 'regexp' },
    { tag: 'url', role: 'string' },
    { tag: 'number', role: 'number' },
    { tag: 'bool', role: 'number' },
    { tag: 'null', role: 'number' },
    { tag: 'atom', role: 'number' },
    { tag: 'unit', role: 'number' },
    { tag: 'color', role: 'number' },
    { tag: 'name', role: 'variable' },
    { tag: 'variableName', role: 'variable' },
    { tag: 'constant(name)', role: 'number' },
    { tag: 'function(variableName)', role: 'function' },
    { tag: 'function(propertyName)', role: 'function' },
    { tag: 'standard(variableName)', role: 'function' },     // builtins
    { tag: 'macroName', role: 'function' },
    { tag: 'propertyName', role: 'variable' },
    { tag: 'labelName', role: 'variable' },
    { tag: 'typeName', role: 'type' },
    { tag: 'className', role: 'type' },
    { tag: 'namespace', role: 'type' },
    { tag: 'tagName', role: 'tag' },
    { tag: 'attributeName', role: 'attribute' },
    { tag: 'operator', role: 'operator' },
    { tag: 'punctuation', role: 'punctuation' },
    { tag: 'invalid', role: 'invalid' },
    { tag: 'inserted', role: 'added' },
    { tag: 'deleted', role: 'removed' },
    { tag: 'changed', role: 'modified' },
    // Markup (a markdown or HTML block inside the document).
    { tag: 'heading', role: 'keyword', fontWeight: '600' },
    { tag: 'emphasis', role: 'variable', fontStyle: 'italic' },
    { tag: 'strong', role: 'variable', fontWeight: '600' },
    { tag: 'strikethrough', role: 'variable', textDecoration: 'line-through' },
    { tag: 'link', role: 'string' },
    { tag: 'monospace', role: 'string' },
    { tag: 'quote', role: 'comment' },
    { tag: 'list', role: 'punctuation' },
    { tag: 'contentSeparator', role: 'punctuation' },
].map((entry) => Object.freeze(entry)));

/** The custom property a palette role's colour is written to. */
export function syntaxVar(role: string): string {
    return `--colmd-syntax-${role}`;
}

/** A tag expression ('variableName', 'function(variableName)') resolved
 *  against a `tags` namespace; null when the namespace lacks a member. */
export function lezerTag(tagsNs: any, expr: string): any {
    const applied = /^(\w+)\((.+)\)$/.exec(expr);
    if (applied) {
        const modifier = tagsNs[applied[1]];
        const inner = lezerTag(tagsNs, applied[2]);
        return typeof modifier === 'function' && inner ? modifier(inner) : null;
    }
    return tagsNs[expr] || null;
}

/** HighlightStyle.define's specs: every LEZER_TAG_ROLES entry the `tags`
 *  namespace has, its colour the role's custom property. */
export function highlightSpecs(tagsNs: any): Array<Record<string, any>> {
    const specs: Array<Record<string, any>> = [];
    for (const { tag, role, ...font } of LEZER_TAG_ROLES) {
        const resolved = lezerTag(tagsNs, tag);
        if (resolved) specs.push({ tag: resolved, color: `var(${syntaxVar(role)})`, ...font });
    }
    return specs;
}

/** Each role's custom property and its value from the palette: the role's
 *  colour, or null (the property is removed) when there is no palette. */
export function syntaxVars(palette: Readonly<Record<string, string>> | null): Record<string, string | null> {
    const out: Record<string, string | null> = {};
    for (const role of SYNTAX_ROLES) out[syntaxVar(role)] = palette ? palette[role] : null;
    return out;
}
