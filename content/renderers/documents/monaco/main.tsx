import React, { useEffect, useRef, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import {
    defineRenderer,
    mirrorUrl,
    resolveVar,
    StyleControl,
    syntaxPalette,
    toHex8,
    useCollimateFile,
    useRemoteScript,
    useStyle,
    useTheme,
} from '/api/sdk/react.js';

const MONACO_BASE = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.57.0/min/vs';
// The loader, editor.main and its workers are classic scripts no import map
// routes, so the whole AMD tree loads from the app's same-origin mirror of
// MONACO_BASE; mirror_assets.json names every file the loader can request.
const MONACO_MIRROR = mirrorUrl(MONACO_BASE);

// Monaco's loader is AMD-style (exposes window.require / window.monaco)
// so we load it as a <script> via useRemoteScript, then block on the
// AMD require for `vs/editor/editor.main`. Cached across mounts by
// useRemoteScript's dedupe table.
let _monacoPromise: Promise<any> | null = null;
function ensureMonaco(): Promise<any> {
    if (_monacoPromise) return _monacoPromise;
    _monacoPromise = new Promise((resolve, reject) => {
        const req = (window as any).require;
        if (!req || !req.config) {
            reject(new Error('monaco loader unavailable'));
            return;
        }
        req.config({ paths: { vs: MONACO_MIRROR } });
        req(
            ['vs/editor/editor.main'],
            () => resolve((window as any).monaco),
            (err: any) => reject(err instanceof Error ? err : new Error(String(err))),
        );
    });
    // A failed load is not cached: a later mount retries editor.main.
    _monacoPromise.catch(() => { _monacoPromise = null; });
    return _monacoPromise;
}

function detectLanguage(content = '', filename = '') {
  const base = filename.split('/').pop().toLowerCase();
  if (base === 'dockerfile') return 'dockerfile';
  if (base === 'makefile') return 'shell';
  const ext = base.split('.').pop();
  const extMap = {
    'js': 'javascript', 'jsx': 'javascript', 'mjs': 'javascript', 'cjs': 'javascript',
    'ts': 'typescript', 'tsx': 'typescript', 'mts': 'typescript', 'cts': 'typescript',
    'py': 'python', 'go': 'go', 'rs': 'rust', 'java': 'java',
    'kt': 'kotlin', 'kts': 'kotlin', 'rb': 'ruby', 'php': 'php',
    'swift': 'swift', 'cs': 'csharp', 'r': 'r', 'pl': 'perl',
    'lua': 'lua', 'dart': 'dart', 'scala': 'scala', 'ps1': 'powershell',
    'c': 'c', 'h': 'c', 'cpp': 'cpp', 'hpp': 'cpp',
    'css': 'css', 'scss': 'scss', 'less': 'less',
    'html': 'html', 'xml': 'xml', 'svg': 'xml',
    'json': 'json', 'jsonc': 'json',
    'md': 'markdown', 'markdown': 'markdown',
    // Monaco 0.57.0 has no TOML language, so TOML falls back to plaintext.
    // Locks are TOML-ish: ini highlighting reads better than plaintext.
    'yaml': 'yaml', 'yml': 'yaml', 'toml': 'toml', 'lock': 'ini',
    'ini': 'ini', 'cfg': 'ini', 'conf': 'ini', 'env': 'ini',
    'gitignore': 'ini', 'gitattributes': 'ini', 'dockerignore': 'ini', 'editorconfig': 'ini',
    'proto': 'protobuf',
    'sql': 'sql', 'graphql': 'graphql', 'gql': 'graphql',
    'sh': 'shell', 'bash': 'shell', 'zsh': 'shell', 'fish': 'shell'
  };
  if (extMap[ext]) return extMap[ext];
  const t = content.trimStart();
  if (/^(import|export|const|let|var|function|class|interface|type )\b/.test(t)) return 'typescript';
  if (/^(import |from |def |class |async def )/.test(t) || t.startsWith('#!/usr/bin/env python')) return 'python';
  if (/^package /.test(t) || /^func /.test(t)) return 'go';
  if (/^(fn |use |struct |impl |pub )/.test(t)) return 'rust';
  if (/^(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP)\b/i.test(t)) return 'sql';
  if (t.startsWith('<') && /<html|<div|<span/i.test(t)) return 'html';
  if (t.startsWith('{') || t.startsWith('[')) return 'json';
  if (/^(#|---\n)/.test(t)) return 'yaml';
  return 'plaintext';
}

// Monaco cannot read CSS variables and takes colours as hex only, so the
// one editor theme, 'collimate', is built from the skin record's resolved
// values (a token with alpha keeps it as #rrggbbaa) and rebuilt on every
// skin change. Syntax colours come from the SDK's syntaxPalette, the same
// roles every highlighter draws code in. `base` (vs or vs-dark by scheme) is
// the built-in defineTheme requires underneath. With a palette the theme sets
// `inherit: false`, which drops the base's own colour table and token rules;
// Monaco's colour registry still supplies a default for every key a theme
// leaves unnamed, so the theme names every key a visible surface uses. With
// no palette (the placeholder, or a skin whose --sheet or --ink cannot be
// read) it inherits the base, so code keeps the base's highlighting and
// foreground until a readable record arrives. The code face is the record's
// mono font, set as an editor option.
export const MONACO_THEME = 'collimate';

type ThemeRecord = {
    scheme: 'dark' | 'light';
    vars: Record<string, string>;
    fonts: { ui: string; mono: string; prose: string };
};

/** syntaxPalette's result: each role ('comment', 'keyword', …) as #rrggbb. */
type SyntaxPalette = Readonly<Record<string, string>>;

/** Every token class Monaco's grammars emit, by the palette role that
 *  draws it. Monaco matches a rule by dot-separated prefix and suffixes
 *  each token with its language id ('keyword.ts', 'tag.html'), so a class
 *  covers its language variants; a variant is listed where it takes a
 *  different role from its prefix. */
export const MONACO_TOKEN_ROLES: Readonly<Record<string, string>> = Object.freeze({
    'comment': 'comment',
    'comment.doc': 'comment',
    'keyword': 'keyword',
    'keyword.flow': 'keyword',
    'keyword.flow.scss': 'keyword',
    'keyword.json': 'number',            // true, false, null
    'string': 'string',
    'string.escape': 'regexp',
    'string.key': 'type',                // a key, as YAML and INI keys
    'string.key.json': 'type',
    'string.value': 'string',
    'string.value.json': 'string',
    'string.html': 'string',
    'string.yaml': 'string',
    'string.sql': 'string',
    'string.link': 'string',
    'number': 'number',
    'number.hex': 'number',
    'number.float': 'number',
    'constant': 'number',
    'regexp': 'regexp',
    'type': 'type',
    'type.identifier': 'type',
    'key': 'type',
    'identifier': 'variable',
    'variable': 'variable',
    'variable.parameter': 'variable',
    'variable.predefined': 'function',   // this, self, builtins
    'predefined': 'function',
    'predefined.sql': 'function',
    'function': 'function',
    'annotation': 'attribute',
    'delimiter': 'punctuation',
    'delimiter.bracket': 'punctuation',
    'delimiter.parenthesis': 'punctuation',
    'delimiter.square': 'punctuation',
    'delimiter.curly': 'punctuation',
    'delimiter.angle': 'punctuation',
    'delimiter.html': 'punctuation',
    'delimiter.xml': 'punctuation',
    'operator': 'operator',
    'operators': 'operator',
    'operator.scss': 'operator',
    'operator.sql': 'operator',
    'operator.swift': 'operator',
    'tag': 'tag',
    'tag.id.pug': 'tag',
    'tag.class.pug': 'tag',
    'metatag': 'tag',
    'metatag.html': 'tag',
    'metatag.xml': 'tag',
    'metatag.php': 'keyword',
    'metatag.content.html': 'string',
    'meta': 'keyword',
    'meta.scss': 'keyword',
    'meta.tag': 'tag',
    'attribute.name': 'attribute',
    'attribute.value': 'string',
    'attribute.value.html': 'string',
    'attribute.value.xml': 'string',
    'attribute.value.number': 'number',
    'attribute.value.unit': 'number',
    'attribute.value.number.css': 'number',
    'attribute.value.unit.css': 'number',
    'attribute.value.hex.css': 'number',
    'invalid': 'invalid',
    'emphasis': 'variable',
    'strong': 'variable',
});

// The style (the style control, top right; the host keeps it per channel
// under this renderer's id): the skin's 'collimate' theme or one of
// Monaco's own, a mono family from the catalogue, and the text size as a
// factor of Monaco's own font size.
export const MONACO_THEMES = Object.freeze([
    Object.freeze({ id: 'skin', label: 'Collimate' }),
    Object.freeze({ id: 'vs', label: 'Visual Studio light' }),
    Object.freeze({ id: 'vs-dark', label: 'Visual Studio dark' }),
    Object.freeze({ id: 'hc-black', label: 'High contrast' }),
]);
const MONACO_OWN_THEMES = new Set(['vs', 'vs-dark', 'hc-black']);

/** The Monaco theme a stored style theme names: one of Monaco's built-ins,
 *  else the skin's 'collimate'. */
export function monacoThemeName(themeId: unknown): string {
    return typeof themeId === 'string' && MONACO_OWN_THEMES.has(themeId) ? themeId : MONACO_THEME;
}

export const MONACO_TEXT_SIZE = Object.freeze({ min: 0.7, max: 2, step: 0.1, default: 1 });

/** The editor's font size for a stored text size, from Monaco's own size
 *  (`base`, read from the editor at creation): `base` itself for null. */
export function monacoFontSize(base: number, textSize: unknown): number {
    if (typeof textSize !== 'number' || !Number.isFinite(textSize)) return base;
    const factor = Math.min(MONACO_TEXT_SIZE.max, Math.max(MONACO_TEXT_SIZE.min, textSize));
    return Math.round(base * factor * 10) / 10;
}

/** The CSS stack of a stored family, from the host's catalogue; null for
 *  none or a family it no longer has (the skin's mono then). */
export function familyStack(family: unknown, families: ReadonlyArray<{ family: string; stack: string }>): string | null {
    if (typeof family !== 'string' || !family) return null;
    const hit = (families || []).find((f) => f && f.family === family);
    return hit && hit.stack ? hit.stack : null;
}

/** What the editor applies of the style: its theme id, the chosen family's
 *  stack and the text size. */
type EditorStyle = { theme: string | null; fontStack: string | null; textSize: number | null };
const NO_STYLE: EditorStyle = Object.freeze({ theme: null, fontStack: null, textSize: null });

/** Font styles Monaco's grammars rely on beyond colour. */
const MONACO_FONT_STYLES: Readonly<Record<string, string>> = Object.freeze({
    emphasis: 'italic',
    strong: 'bold',
    'metatag.php': 'bold',
});

/** The editor options that put code on the record's mono font; empty when
 *  the record names none (the placeholder), leaving Monaco's own stack. */
export function monacoFontOptions(theme: ThemeRecord): { fontFamily?: string } {
    const mono = theme.fonts && theme.fonts.mono;
    return mono ? { fontFamily: mono } : {};
}

/** `color` with the alpha of `soft` (both #rrggbbaa): a soft fill for a
 *  colour the skin has no soft variant of, matched to one it has. */
function withAlphaOf(color: string | undefined, soft: string | undefined): string | undefined {
    if (!color || !soft) return undefined;
    return color.slice(0, 7) + soft.slice(7, 9);
}

/** Monaco's token rules from the palette: one rule per token class. */
export function monacoTokenRules(palette: SyntaxPalette | null) {
    if (!palette) return [];
    return Object.entries(MONACO_TOKEN_ROLES).map(([token, role]) => ({
        token,
        foreground: palette[role],
        ...(MONACO_FONT_STYLES[token] ? { fontStyle: MONACO_FONT_STYLES[token] } : {}),
    }));
}

export function monacoThemeData(theme: ThemeRecord) {
    const hex = (name: string) => toHex8(resolveVar(theme as any, name as any)) || undefined;
    const sheet = hex('--sheet');
    const sheet2 = hex('--sheet-2');
    const sheet3 = hex('--sheet-3');
    const line = hex('--line');
    const line2 = hex('--line-2');
    const ink = hex('--ink');
    const ink2 = hex('--ink-2');
    const ink3 = hex('--ink-3');
    const brass = hex('--brass');
    const brassSoft = hex('--brass-soft');
    const teal = hex('--teal');
    const tealSoft = hex('--teal-soft');
    const red = hex('--red');
    const redSoft = hex('--red-soft');
    const codeBg = hex('--code-bg');
    const lift = hex('--lift');
    const palette: SyntaxPalette | null = syntaxPalette(theme as any);
    const role = (name: string) => (palette ? toHex8(palette[name]) || undefined : undefined);
    // The skin's soft alpha (red-soft's) for fills it has no soft token for.
    const soft = (color: string | undefined) => withAlphaOf(color, redSoft);
    const colors: Record<string, string | undefined> = {
        'editor.background': sheet,
        'editor.foreground': ink,
        'editor.lineHighlightBackground': sheet2,
        'editor.lineHighlightBorder': sheet2,
        'editor.selectionBackground': brassSoft,
        'editor.inactiveSelectionBackground': brassSoft,
        'editor.selectionHighlightBackground': sheet3,
        'editor.wordHighlightBackground': sheet3,
        'editor.wordHighlightStrongBackground': sheet3,
        'editor.findMatchBackground': tealSoft,
        'editor.findMatchHighlightBackground': tealSoft,
        'editor.findMatchBorder': teal,
        'editor.findRangeHighlightBackground': sheet2,
        'editorCursor.foreground': teal,
        'editorGutter.background': sheet,
        'editorGutter.foldingControlForeground': ink3,
        'editorLineNumber.foreground': ink3,
        'editorLineNumber.activeForeground': ink2,
        'editorIndentGuide.background1': line,
        'editorIndentGuide.activeBackground1': line2,
        'editorWhitespace.foreground': line2,
        'editorRuler.foreground': line,
        'editorOverviewRuler.border': line,
        'editorOverviewRuler.findMatchForeground': teal,
        'editorOverviewRuler.selectionHighlightForeground': soft(ink3),
        'editorOverviewRuler.wordHighlightForeground': soft(ink3),
        'editorOverviewRuler.errorForeground': red,
        'editorOverviewRuler.warningForeground': brass,
        'editorOverviewRuler.infoForeground': teal,
        'editorOverviewRuler.bracketMatchForeground': line2,
        'editorOverviewRuler.rangeHighlightForeground': soft(ink3),
        'editorOverviewRuler.addedForeground': role('added'),
        'editorOverviewRuler.deletedForeground': role('removed'),
        'editorOverviewRuler.modifiedForeground': role('modified'),
        'editorError.foreground': red,
        'editorWarning.foreground': brass,
        'editorInfo.foreground': teal,
        'editorLink.activeForeground': teal,
        'editorLightBulb.foreground': brass,
        'editorLightBulbAutoFix.foreground': teal,
        'editorGhostText.foreground': ink3,
        'editor.hoverHighlightBackground': sheet3,
        'editor.rangeHighlightBackground': sheet2,
        'editorMarkerNavigation.background': sheet2,
        'editorMarkerNavigationError.background': red,
        'editorMarkerNavigationWarning.background': brass,
        'editorMarkerNavigationInfo.background': teal,
        'editorBracketMatch.background': sheet3,
        'editorBracketMatch.border': line2,
        // Bracket pairs cycle through the skin's three code hues.
        'editorBracketHighlight.foreground1': role('keyword'),
        'editorBracketHighlight.foreground2': role('number'),
        'editorBracketHighlight.foreground3': role('string'),
        'editorBracketHighlight.unexpectedBracket.foreground': role('invalid'),
        'editorStickyScroll.background': sheet,
        'editorStickyScrollHover.background': sheet2,
        'scrollbar.shadow': lift,
        // Opaque, so the thumb reads on every ground (a soft fill does not).
        'scrollbarSlider.background': line2,
        'scrollbarSlider.hoverBackground': ink3,
        'scrollbarSlider.activeBackground': ink2,
        'widget.shadow': lift,
        'focusBorder': teal,
        'editorWidget.background': sheet2,
        'editorWidget.foreground': ink,
        'editorWidget.border': line2,
        'editorHoverWidget.background': sheet2,
        'editorHoverWidget.foreground': ink,
        'editorHoverWidget.border': line2,
        'textLink.foreground': teal,
        'textLink.activeForeground': teal,
        'textPreformat.foreground': ink2,
        'textCodeBlock.background': codeBg,
        'keybindingLabel.foreground': ink2,
        'keybindingLabel.background': sheet3,
        'keybindingLabel.border': line2,
        'keybindingLabel.bottomBorder': line2,
        'peekView.border': teal,
        'peekViewTitle.background': sheet2,
        'peekViewTitleLabel.foreground': ink,
        'peekViewTitleDescription.foreground': ink2,
        'peekViewEditor.background': sheet,
        'peekViewEditorGutter.background': sheet,
        'peekViewEditor.matchHighlightBackground': tealSoft,
        'peekViewResult.background': sheet2,
        'peekViewResult.fileForeground': ink,
        'peekViewResult.lineForeground': ink2,
        'peekViewResult.selectionBackground': brassSoft,
        'peekViewResult.selectionForeground': ink,
        'peekViewResult.matchHighlightBackground': tealSoft,
        'editorSuggestWidget.background': sheet2,
        'editorSuggestWidget.foreground': ink,
        'editorSuggestWidget.border': line2,
        'editorSuggestWidget.selectedBackground': brassSoft,
        'editorSuggestWidget.selectedForeground': ink,
        'editorSuggestWidget.highlightForeground': teal,
        'list.hoverBackground': sheet3,
        'list.activeSelectionBackground': brassSoft,
        'list.activeSelectionForeground': ink,
        'list.focusBackground': brassSoft,
        'list.highlightForeground': teal,
        'list.inactiveSelectionBackground': sheet3,
        'list.focusOutline': teal,
        'menu.background': sheet2,
        'menu.foreground': ink,
        'menu.border': line2,
        'menu.selectionBackground': brassSoft,
        'menu.selectionForeground': ink,
        'menu.separatorBackground': line,
        'quickInput.background': sheet2,
        'quickInput.foreground': ink,
        'quickInputList.focusBackground': brassSoft,
        'quickInputList.focusForeground': ink,
        'pickerGroup.foreground': teal,
        'pickerGroup.border': line,
        'input.background': sheet,
        'input.foreground': ink,
        'input.border': line2,
        'input.placeholderForeground': ink3,
        'inputOption.activeBorder': teal,
        'inputOption.activeBackground': tealSoft,
        'inputOption.activeForeground': ink,
        // Diff fills: the palette's added and removed at the skin's soft alpha.
        'diffEditor.insertedTextBackground': soft(role('added')),
        'diffEditor.insertedLineBackground': soft(role('added')),
        'diffEditor.removedTextBackground': soft(role('removed')),
        'diffEditor.removedLineBackground': soft(role('removed')),
        'diffEditorGutter.insertedLineBackground': soft(role('added')),
        'diffEditorGutter.removedLineBackground': soft(role('removed')),
        'diffEditorOverview.insertedForeground': soft(role('added')),
        'diffEditorOverview.removedForeground': soft(role('removed')),
        'diffEditor.unchangedRegionBackground': sheet2,
        'diffEditor.unchangedRegionForeground': ink2,
        'diffEditor.border': line,
        'diffEditor.diagonalFill': line,
        'editorGutter.addedBackground': role('added'),
        'editorGutter.deletedBackground': role('removed'),
        'editorGutter.modifiedBackground': role('modified'),
    };
    return {
        // defineTheme requires a built-in base; the scheme picks it. Without
        // a palette the base's token rules and colours stand in.
        base: theme.scheme === 'light' ? 'vs' : 'vs-dark',
        inherit: palette === null,
        rules: monacoTokenRules(palette),
        colors: Object.fromEntries(
            Object.entries(colors).filter((entry): entry is [string, string] => !!entry[1]),
        ),
    };
}

/** Monaco measures glyph widths once per font; a face that loads later
 *  leaves the cursor and selections misplaced, so once the mono face has
 *  loaded the measurements are taken again. */
function remeasureWhenLoaded(monaco: any, family: string | undefined) {
    const fonts = (document as any).fonts;
    if (!fonts) return;
    const loaded = family ? fonts.load(`14px ${family}`).catch(() => []) : Promise.resolve();
    loaded
        .then(() => fonts.ready)
        .then(() => { try { monaco.editor.remeasureFonts(); } catch {} });
}

/** The editor's font options under a style: the chosen family's stack,
 *  else the record's mono font. */
export function monacoEditorFont(theme: ThemeRecord, fontStack: string | null): { fontFamily?: string } {
    return fontStack ? { fontFamily: fontStack } : monacoFontOptions(theme);
}

/** Rebuild the 'collimate' theme from the record, show the style's theme
 *  (the skin's, or one of Monaco's own) and put `editor` (the code editor
 *  or the diff editor) on the style's font, else the record's mono, at the
 *  style's size of Monaco's own (`baseSize`). */
function applyMonacoTheme(monaco: any, theme: ThemeRecord, editor?: any, style: EditorStyle = NO_STYLE, baseSize = 0) {
    try {
        monaco.editor.defineTheme(MONACO_THEME, monacoThemeData(theme));
        monaco.editor.setTheme(monacoThemeName(style.theme));
    } catch {}
    const font = monacoEditorFont(theme, style.fontStack);
    if (editor) {
        const options: Record<string, unknown> = { ...font };
        if (baseSize > 0) options.fontSize = monacoFontSize(baseSize, style.textSize);
        try { editor.updateOptions(options); } catch {}
    }
    remeasureWhenLoaded(monaco, font.fontFamily);
}

/** The style as the editor applies it, from the host's record and font
 *  catalogue. */
function useEditorStyle(api: any) {
    const style = useStyle(api);
    const families = api && api.getFontFamilies ? api.getFontFamilies() : [];
    const current: EditorStyle = {
        theme: style.theme, fontStack: familyStack(style.font, families), textSize: style.textSize,
    };
    const ref = useRef<EditorStyle>(current);
    ref.current = current;
    return { ref, key: `${current.theme}|${current.fontStack}|${current.textSize}` };
}

/** The editor's own font size (Monaco's platform default), read once it
 *  exists, before the style scales it. */
function baseFontSize(monaco: any, editor: any): number {
    try {
        const size = editor.getOption(monaco.editor.EditorOption.fontSize);
        return typeof size === 'number' && size > 0 ? size : 14;
    } catch {
        return 14;
    }
}

/** The style control's place: top right, clear of the vertical scrollbar. */
const STYLE_CONTROL_PLACE = Object.freeze({ top: 6, right: 22 });

/** The editor's box: it fills the renderer's box. */
export const EDITOR_BOX = Object.freeze({
    width: '100%', height: '100%', backgroundColor: 'var(--sheet)',
});

/** Monaco's own box inside EDITOR_BOX, beside the style control. */
const MONACO_BOX = Object.freeze({ width: '100%', height: '100%' });

const loadErrorStyle = {
    padding: 16,
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    color: 'var(--red)',
};

// A press below the last line puts the caret at the end: Monaco 0.57.0 does
// this itself (its mouse target for the area after the lines is CONTENT_EMPTY
// at the last line's last column, and a press on a content target focuses
// the editor and moves the cursor there), so the renderer adds nothing.
function MonacoEditor() {
    const { content, saveContent, isReadOnly, fileName, api } = useCollimateFile();
    const { scheme, vars, fonts, onChange: onThemeChange } = useTheme(api);
    const themeRef = useRef<ThemeRecord>({ scheme, vars, fonts });
    themeRef.current = { scheme, vars, fonts };
    const { ref: styleRef, key: styleKey } = useEditorStyle(api);
    // The live editor, for a style change (the mount effect owns its life).
    const liveRef = useRef<{ monaco: any; editor: any; base: number } | null>(null);
    const { loading: loaderLoading, error: loaderError } = useRemoteScript(`${MONACO_MIRROR}/loader.js`);
    const [loadError, setLoadError] = useState<string | null>(null);
    const containerRef = useRef<HTMLDivElement | null>(null);
    const saveRef = useRef(saveContent);
    saveRef.current = saveContent;

    useEffect(() => {
        if (loaderLoading || !containerRef.current) return;
        if (loaderError) {
            setLoadError(loaderError.message);
            if (api && api.ready) api.ready();
            return;
        }
        let editor: any;
        let unsubTheme: (() => void) | undefined;
        let unsubFlush: (() => void) | undefined;
        let hostContent = content;
        let applyingHost = false;
        let pending = false;
        const clearPending = () => {
            pending = false;
        };
        const flush = () => {
            if (!pending || !editor) return;
            clearPending();
            const value = editor.getValue();
            saveRef.current(value);
        };
        // Only an explicit host replacement changes the mounted model. Local
        // React content commits may lag behind a newer editor transaction.
        const unsubReplace = api.onSetContent((value: string) => {
            hostContent = value;
            clearPending();
            const model = editor?.getModel();
            if (!model || model.getValue() === value) return;
            const view = editor.saveViewState();
            applyingHost = true;
            try {
                model.pushEditOperations([], [{ range: model.getFullModelRange(), text: value }], () => null);
            } finally {
                applyingHost = false;
            }
            if (view) editor.restoreViewState(view);
        });
        let cancelled = false;
        ensureMonaco().then((monaco) => {
            if (cancelled || !containerRef.current) return;
            setLoadError(null);
            applyMonacoTheme(monaco, themeRef.current, undefined, styleRef.current);
            editor = monaco.editor.create(containerRef.current, {
                value: hostContent || '',
                language: detectLanguage(hostContent, fileName),
                theme: monacoThemeName(styleRef.current.theme),
                ...monacoEditorFont(themeRef.current, styleRef.current.fontStack),
                readOnly: isReadOnly,
                automaticLayout: true,
                minimap: { enabled: false },
            });
            const base = baseFontSize(monaco, editor);
            liveRef.current = { monaco, editor, base };
            applyMonacoTheme(monaco, themeRef.current, editor, styleRef.current, base);
            if (!isReadOnly) {
                // A body the replacement listener applies came from the host, so
                // it must not echo back as an edit.
                editor.onDidChangeModelContent((e: any) => {
                    if (e.isFlush || applyingHost) { clearPending(); return; }
                    pending = true;
                    // Every size reaches the host's durable journal now.
                    flush();
                });
                unsubFlush = api.onFlush?.(flush);
            }
            unsubTheme = onThemeChange((t) => applyMonacoTheme(monaco, t, editor, styleRef.current, base));
            requestAnimationFrame(() => { if (!cancelled && api && api.ready) api.ready(); });
        }).catch((err) => {
            if (cancelled) return;
            setLoadError(String(err?.message || err));
            if (api && api.ready) api.ready();
        });
        return () => {
            cancelled = true;
            liveRef.current = null;
            // The host's awaited onFlush barrier serializes before disposal.
            // External replacements clear pending work and skip that flush.
            clearPending();
            unsubFlush?.();
            unsubReplace();
            if (unsubTheme) unsubTheme();
            if (editor) editor.dispose();
        };
    }, [loaderLoading, loaderError]);

    // A style change reaches the live editor at once.
    useEffect(() => {
        const live = liveRef.current;
        if (live) applyMonacoTheme(live.monaco, themeRef.current, live.editor, styleRef.current, live.base);
    }, [styleKey]);

    return (
        <div style={{ ...EDITOR_BOX, position: 'relative' }}>
            <div ref={containerRef} style={MONACO_BOX}>
                {loadError && <div style={loadErrorStyle}>Monaco failed to load: {loadError}</div>}
            </div>
            {/* Shown while the pointer is near its corner (it watches this
                box, its parent). */}
            <StyleControl api={api} themes={MONACO_THEMES} font="mono" size={MONACO_TEXT_SIZE}
                style={STYLE_CONTROL_PLACE} />
        </div>
    );
}

function MonacoDiffEditor() {
    const { content, diffContent, fileName, api } = useCollimateFile();
    const { scheme, vars, fonts, onChange: onThemeChange } = useTheme(api);
    const themeRef = useRef<ThemeRecord>({ scheme, vars, fonts });
    themeRef.current = { scheme, vars, fonts };
    // The code editor's style, applied here too (the control is the code
    // editor's).
    const { ref: styleRef, key: styleKey } = useEditorStyle(api);
    const liveRef = useRef<{ monaco: any; editor: any; base: number } | null>(null);
    const { loading: loaderLoading, error: loaderError } = useRemoteScript(`${MONACO_MIRROR}/loader.js`);
    const [loadError, setLoadError] = useState<string | null>(null);
    const containerRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (loaderLoading || !containerRef.current) return;
        if (loaderError) {
            setLoadError(loaderError.message);
            if (api && api.ready) api.ready();
            return;
        }
        let diffEditor: any;
        let originalModel: any;
        let modifiedModel: any;
        let unsubTheme: (() => void) | undefined;
        let cancelled = false;
        ensureMonaco().then((monaco) => {
            if (cancelled || !containerRef.current) return;
            setLoadError(null);
            applyMonacoTheme(monaco, themeRef.current, undefined, styleRef.current);
            diffEditor = monaco.editor.createDiffEditor(containerRef.current, {
                theme: monacoThemeName(styleRef.current.theme),
                ...monacoEditorFont(themeRef.current, styleRef.current.fontStack),
                automaticLayout: true,
                renderSideBySide: true,
                readOnly: true,
            });
            const lang = detectLanguage(content, fileName);
            originalModel = monaco.editor.createModel(diffContent || '', lang);
            modifiedModel = monaco.editor.createModel(content || '', lang);
            diffEditor.setModel({ original: originalModel, modified: modifiedModel });
            const base = baseFontSize(monaco, diffEditor.getModifiedEditor());
            liveRef.current = { monaco, editor: diffEditor, base };
            applyMonacoTheme(monaco, themeRef.current, diffEditor, styleRef.current, base);
            unsubTheme = onThemeChange((t) => applyMonacoTheme(monaco, t, diffEditor, styleRef.current, base));
            requestAnimationFrame(() => { if (!cancelled && api && api.ready) api.ready(); });
        }).catch((err) => {
            if (cancelled) return;
            setLoadError(String(err?.message || err));
            if (api && api.ready) api.ready();
        });
        return () => {
            cancelled = true;
            liveRef.current = null;
            if (unsubTheme) unsubTheme();
            if (diffEditor) diffEditor.dispose();
            // The diff editor does not own models it was handed: dispose them
            // after it, or each run leaves two copies in monaco's registry.
            try { originalModel?.dispose(); } catch {}
            try { modifiedModel?.dispose(); } catch {}
        };
    }, [content, diffContent, fileName, loaderLoading, loaderError]);

    useEffect(() => {
        const live = liveRef.current;
        if (live) applyMonacoTheme(live.monaco, themeRef.current, live.editor, styleRef.current, live.base);
    }, [styleKey]);

    return (
        <div ref={containerRef} style={EDITOR_BOX}>
            {loadError && <div style={loadErrorStyle}>Monaco failed to load: {loadError}</div>}
        </div>
    );
}

export default defineRenderer({
    component: MonacoEditor,
    diffComponent: MonacoDiffEditor,
    deferReady: true,
});
