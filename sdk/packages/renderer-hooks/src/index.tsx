// SPDX-License-Identifier: MIT
// Canonical SDK-owned hooks, built into /api/sdk/react.js at vendor time.
// npm consumers use this same implementation.
//
// Exports:
//   useTheme(api)              — { themeId, scheme, known, vars, fonts, onChange }
//   useEditing(api)            — { spellcheck, onChange } the user's editing preferences
//   useStyle(api)              — { theme, font, textSize, set, onChange } this
//                                renderer's style, persisted per channel
//   StyleControl               — the corner style icon (shown near the pointer) and its popover,
//                                with the renderer's own rows under it (`extra`,
//                                `StyleControlRow`) (./style-control)
//   CELL_FILES_MIME, isCellFileDrag(dt), readCellFileDrop(dt, api)
//                              — a drag of this cell's files from the files pane
//   tokens                     — the skin tokens as var(--…) references
//   themeCss(theme, opts?)     — :root CSS for a renderer-owned opaque frame
//   resolveVar / toHex8        — a token's value; a CSS colour as #rrggbbaa
//   syntaxPalette(theme)       — code-highlighting colours derived from the skin
//   useDesktop(api)            — desktop-shell surface, or undefined
//   useVisibility(api)         — { visible, onChange } cell on/off-screen handle
//   useHostFocus(api)          — { focused, onChange } cell canvas-focus handle
//   useAnnotationRequest(api, handler)
//                              — answer the host's annotation requests (once
//                                per api, the latest handler)
//   annotationUnavailable(message)
//                              — the rejection for 'this view cannot answer'
//   useRemoteModule(url)       — dynamic ESM loader with dedupe + cache
//   useRemoteScript(url)       — dynamic <script> loader with dedupe
//   useDocumentView(…)         — an authored HTML document in an opaque frame:
//                                DarkReader on the skin, the backing, the
//                                element picker (./document-view)
//
// Session sockets (openSessionSocket, sessionWsUrl, …) live in ./session —
// framework-free, served by the platform at `/api/sdk/session.js` so
// renderers that pin their own React (or use none) can import them without
// pulling a second React instance. Re-exported here for npm consumers.
//
// Host-mounted primitives are part of this same SDK source.
export { defineRenderer, useCollimateFile, useSiblingFile } from './platform';

export * from './session';

export type {
    AnnotationContext,
    AnnotationRequest,
    CollimateFile,
    DefineRendererConfig,
    DesktopCapabilities,
    DesktopSurface,
    FontRole,
    GuestEventData,
    GuestHandle,
    GuestOptions,
    RendererAPI,
    RendererDocumentSnapshot,
    RendererRecoverySnapshot,
    RendererSnapshotCodec,
    RendererDefinition,
    RendererEditing,
    RendererFontFamily,
    RendererPreview,
    RendererStyle,
    RendererTheme,
    Scheme,
    SiblingFileState,
    SurfaceSession,
    ThemeFonts,
    ThemeVarName,
} from './types';

import type {
    AnnotationContext, AnnotationRequest,
    DesktopSurface, RendererEditing, RendererFontFamily, RendererStyle, RendererTheme, Scheme, ThemeFonts, ThemeVarName,
} from './types';

import {
    useCallback,
    useEffect,
    useRef,
    useState,
} from 'react';

// ---------------------------------------------------------------------------
// tokens — the skin tokens as `var(--…)` references.
//
// The host writes every token on the renderer's :root, and CSS custom
// properties inherit into a shadow root, so these strings work in an inline
// style in both embed modes and follow every skin change with no
// subscription. Brass marks the selection and nothing else; teal is the
// live / on-state; red is destructive; green is pass.
// ---------------------------------------------------------------------------

export const tokens = Object.freeze({
    color: Object.freeze({
        canvas:    'var(--canvas)',
        sheet:     'var(--sheet)',
        sheet2:    'var(--sheet-2)',
        sheet3:    'var(--sheet-3)',
        line:      'var(--line)',
        line2:     'var(--line-2)',
        ink:       'var(--ink)',
        ink2:      'var(--ink-2)',
        ink3:      'var(--ink-3)',
        brass:     'var(--brass)',
        brassSoft: 'var(--brass-soft)',
        teal:      'var(--teal)',
        tealSoft:  'var(--teal-soft)',
        tealSheet: 'var(--teal-sheet)',
        red:       'var(--red)',
        redSoft:   'var(--red-soft)',
        green:     'var(--green)',
        codeBg:    'var(--code-bg)',
        proseInk:  'var(--prose-ink)',
    }),
    font: Object.freeze({
        ui:    'var(--font-ui)',
        mono:  'var(--font-mono)',
        prose: 'var(--font-prose)',
    }),
    radius: 'var(--radius)',
    shadow: 'var(--shadow)',
    lift:   'var(--lift)',
});

/** Every token's CSS name, in the order `themeCss` writes them. */
export const THEME_VAR_NAMES: readonly ThemeVarName[] = Object.freeze([
    '--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2',
    '--ink', '--ink-2', '--ink-3', '--brass', '--brass-soft',
    '--teal', '--teal-soft', '--teal-sheet', '--red', '--red-soft', '--green',
    '--code-bg', '--prose-ink', '--shadow', '--lift',
    '--font-ui', '--font-mono', '--font-prose', '--radius',
] as const);

// ---------------------------------------------------------------------------
// useTheme — the host's skin as React state, plus a subscription handle.
// ---------------------------------------------------------------------------

/** What `getTheme()` returns before any host theme exists. */
const PLACEHOLDER_THEME: RendererTheme = Object.freeze({
    themeId: 'unknown',
    scheme: 'dark' as Scheme,
    known: false,
    vars: Object.freeze({}) as Record<ThemeVarName, string>,
    fonts: Object.freeze({ ui: '', mono: '', prose: '' }),
});

export interface ThemeApi {
    getTheme?: () => RendererTheme;
    onThemeChange?: (cb: (theme: RendererTheme) => void) => (() => void) | undefined;
}

export interface ThemeHandle {
    themeId: string;
    scheme: Scheme;
    /** False until the host's first theme arrives (see RendererTheme.known). */
    known: boolean;
    vars: Record<ThemeVarName, string>;
    fonts: ThemeFonts;
    /** Subscribe to the full snapshot; fires immediately, then on every
     *  change. Returns unsubscribe (undefined when the api has no theme). */
    onChange: (cb: (theme: RendererTheme) => void) => (() => void) | undefined;
}

export function useTheme(api: ThemeApi | null | undefined): ThemeHandle {
    const getInitial = (): RendererTheme =>
        (api && api.getTheme ? api.getTheme() : PLACEHOLDER_THEME);
    const [theme, setTheme] = useState<RendererTheme>(getInitial);
    useEffect(() => {
        if (!api || !api.onThemeChange) return;
        return api.onThemeChange((t) => setTheme(t));
    }, [api]);
    const onChange = useCallback(
        (cb: (t: RendererTheme) => void) => {
            if (!api || !api.onThemeChange) return undefined;
            return api.onThemeChange(cb);
        },
        [api],
    );
    return {
        themeId: theme.themeId,
        scheme: theme.scheme,
        known: theme.known,
        vars: theme.vars,
        fonts: theme.fonts,
        onChange,
    };
}

// ---------------------------------------------------------------------------
// useEditing — the user's editing preferences as React state, plus a
// subscription handle. Mirrors useTheme.
//
// The host already sets spellcheck on the renderer's document, so a plain
// <textarea> or contenteditable that leaves the attribute unset follows it
// with no code. Read this for an editor that sets its own attribute or runs
// in a frame of its own (a srcdoc editor), and pass the flag through:
//
//     const { spellcheck } = useEditing(api);
//     useEffect(() => {
//         frame.contentWindow?.postMessage({ type: 'editing', spellcheck }, '*');
//     }, [spellcheck]);
//
// Degrades safely: an api without the editing members reports the
// browser default, spellcheck on.
// ---------------------------------------------------------------------------

/** What `getEditing()` reports before any host preferences exist. */
const DEFAULT_EDITING: RendererEditing = Object.freeze({ spellcheck: true });

export interface EditingApi {
    getEditing?: () => RendererEditing;
    onEditingChange?: (cb: (editing: RendererEditing) => void) => (() => void) | undefined;
}

export interface EditingHandle {
    spellcheck: boolean;
    /** Subscribe to the full record; fires immediately, then on every
     *  change. Returns unsubscribe (undefined when the api has no editing
     *  preferences). */
    onChange: (cb: (editing: RendererEditing) => void) => (() => void) | undefined;
}

export function useEditing(api: EditingApi | null | undefined): EditingHandle {
    const getInitial = (): RendererEditing =>
        (api && api.getEditing ? api.getEditing() : DEFAULT_EDITING);
    const [editing, setEditing] = useState<RendererEditing>(getInitial);
    useEffect(() => {
        if (!api || !api.onEditingChange) return undefined;
        return api.onEditingChange((e) => setEditing(e));
    }, [api]);
    const onChange = useCallback(
        (cb: (e: RendererEditing) => void) => {
            if (!api || !api.onEditingChange) return undefined;
            return api.onEditingChange(cb);
        },
        [api],
    );
    return { spellcheck: editing.spellcheck !== false, onChange };
}

// ---------------------------------------------------------------------------
// useStyle — this renderer's style (theme, font, text size) as React state,
// plus a setter and a subscription handle. Mirrors useEditing.
//
// The host persists the record per channel under the renderer's id, and a
// null field is the renderer's default (the skin, the skin's font for the
// role, the renderer's own size), so a renderer resolves each one itself:
//
//     const style = useStyle(api);
//     const zoom = style.textSize ?? 1.1;
//     const family = api.getFontFamilies().find((f) => f.family === style.font);
//     const font = family ? family.stack : 'var(--font-prose)';
//
// `StyleControl` (./style-control) is the shared UI that writes it.
// Degrades safely: an api without the style members reads all-null and
// `set` does nothing.
// ---------------------------------------------------------------------------

/** What `getStyle()` reports before any host record exists. */
const DEFAULT_STYLE: RendererStyle = Object.freeze({ theme: null, font: null, textSize: null });

export interface StyleApi {
    getStyle?: () => RendererStyle;
    onStyleChange?: (cb: (style: RendererStyle) => void) => (() => void) | undefined;
    setStyle?: (patch: Partial<RendererStyle>) => void;
    getFontFamilies?: () => ReadonlyArray<RendererFontFamily>;
}

export interface StyleHandle {
    theme: string | null;
    font: string | null;
    textSize: number | null;
    /** Change some fields (null puts one back to the default); the host
     *  persists the record and pushes it back through `onChange`. */
    set: (patch: Partial<RendererStyle>) => void;
    /** Subscribe to the full record; fires immediately, then on every
     *  change. Returns unsubscribe (undefined when the api has no style). */
    onChange: (cb: (style: RendererStyle) => void) => (() => void) | undefined;
}

/** A record from the host as the three fields, each a real value or null. */
function styleOf(style: Partial<RendererStyle> | null | undefined): RendererStyle {
    if (!style || typeof style !== 'object') return DEFAULT_STYLE;
    const { theme, font, textSize } = style;
    return {
        theme: typeof theme === 'string' && theme ? theme : null,
        font: typeof font === 'string' && font ? font : null,
        textSize: typeof textSize === 'number' && Number.isFinite(textSize) && textSize > 0 ? textSize : null,
    };
}

export function useStyle(api: StyleApi | null | undefined): StyleHandle {
    const getInitial = (): RendererStyle =>
        (api && api.getStyle ? styleOf(api.getStyle()) : DEFAULT_STYLE);
    const [style, setStyle] = useState<RendererStyle>(getInitial);
    useEffect(() => {
        if (!api || !api.onStyleChange) return undefined;
        return api.onStyleChange((s) => setStyle(styleOf(s)));
    }, [api]);
    const set = useCallback(
        (patch: Partial<RendererStyle>) => {
            if (api && api.setStyle) api.setStyle(patch);
        },
        [api],
    );
    const onChange = useCallback(
        (cb: (s: RendererStyle) => void) => {
            if (!api || !api.onStyleChange) return undefined;
            return api.onStyleChange((s) => cb(styleOf(s)));
        },
        [api],
    );
    return { theme: style.theme, font: style.font, textSize: style.textSize, set, onChange };
}

// ---------------------------------------------------------------------------
// Dropping a file of this cell from the files pane.
//
// The files pane's drag carries `{cellId, paths}` as JSON under
// CELL_FILES_MIME (cell-relative paths). A drop target accepts the drag in
// `dragover` from the type alone (a page cannot read drag data before the
// drop), then reads it on `drop`, where only this cell's files come back:
//
//     el.addEventListener('dragover', (e) => {
//         if (!isCellFileDrag(e.dataTransfer)) return;
//         e.preventDefault();
//         e.dataTransfer.dropEffect = 'copy';
//     });
//     el.addEventListener('drop', (e) => {
//         const paths = readCellFileDrop(e.dataTransfer, api);
//         if (!paths) return;            // another cell's files, or no drag of ours
//         e.preventDefault();
//         for (const path of paths) insertLink(path);
//     });
// ---------------------------------------------------------------------------

/** The drag type the files pane writes its payload under. */
export const CELL_FILES_MIME = 'application/x-collimate-files';

/** Whether a drag carries files from the files pane. Reads only the types,
 *  so it works in `dragenter` and `dragover` too. */
export function isCellFileDrag(dt: DataTransfer | null | undefined): boolean {
    const types = dt && dt.types;
    if (!types) return false;
    for (let i = 0; i < types.length; i++) if (types[i] === CELL_FILES_MIME) return true;
    return false;
}

export interface CellDropApi {
    getCellId?: () => string;
}

/** The dropped paths (cell-relative) when the drag carries files of THIS
 *  renderer's cell; null for another cell's files, a malformed payload, or
 *  a drag that is not the files pane's. Call it in `drop`. */
export function readCellFileDrop(dt: DataTransfer | null | undefined, api: CellDropApi | null | undefined): string[] | null {
    if (!dt || !isCellFileDrag(dt)) return null;
    const cellId = api && api.getCellId ? api.getCellId() : '';
    if (!cellId) return null;
    let payload: unknown;
    try {
        payload = JSON.parse(dt.getData(CELL_FILES_MIME));
    } catch {
        return null;
    }
    if (!payload || typeof payload !== 'object') return null;
    const { cellId: from, paths } = payload as { cellId?: unknown; paths?: unknown };
    if (from !== cellId || !Array.isArray(paths)) return null;
    const own = paths.filter((p): p is string => typeof p === 'string' && p !== '');
    return own.length ? own : null;
}

// ---------------------------------------------------------------------------
// themeCss — the skin as CSS text for a frame the renderer owns.
//
// A nested srcdoc/sandboxed frame (a preview, an embedded editor) is its own
// document: the host's :root variables do not reach it. Inject this into the
// frame's <head> so the frame's `var(--sheet)` and font stacks match the
// host. `fontsUrl` is the host's font stylesheet, absolute because an opaque
// frame has no base to resolve a relative URL against:
//
//     themeCss(theme, { fontsUrl: new URL('/fonts/fonts.css', location.href).href })
//
// It writes variables and color-scheme only; the frame's own rules decide
// what uses them.
// ---------------------------------------------------------------------------

function cssString(value: string): string {
    return '"' + value.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\n\r\f]/g, ' ') + '"';
}

export interface ThemeCssOptions {
    /** Absolute URL of the host's font stylesheet, imported first. */
    fontsUrl?: string;
}

export function themeCss(theme: RendererTheme, opts: ThemeCssOptions = {}): string {
    const parts: string[] = [];
    if (opts.fontsUrl) parts.push(`@import url(${cssString(opts.fontsUrl)});`);
    const vars = (theme && theme.vars) || ({} as Record<string, string>);
    const decls: string[] = [];
    for (const name of THEME_VAR_NAMES) {
        const value = vars[name];
        // A value that could close the rule or the <style> element is not a
        // token value; leave it out rather than break the frame's CSS.
        if (typeof value !== 'string' || !value.trim() || /[;{}<>]/.test(value)) continue;
        decls.push(`${name}:${value.trim()};`);
    }
    decls.push(`color-scheme:${theme && theme.scheme === 'light' ? 'light' : 'dark'};`);
    parts.push(`:root{${decls.join('')}}`);
    return parts.join('\n');
}

// ---------------------------------------------------------------------------
// Concrete colours for libraries that cannot read CSS variables: a token's
// value, a CSS colour as #rrggbbaa (./color), and the syntax palette derived
// from the skin (./syntax).
// ---------------------------------------------------------------------------

export { resolveVar, toHex8 } from './color';
export { SYNTAX_ROLES, syntaxPalette } from './syntax';
export type { SyntaxPalette, SyntaxRole } from './syntax';

// ---------------------------------------------------------------------------
// useDesktop — the Electron shell surface, or undefined in a browser.
//
// Deliberately not a boolean "isDesktop": the useful question is never
// "am I on the desktop" but "do I have this handle", and returning the
// handle answers both at once. `undefined` is the browser case AND the
// sandboxed-iframe case (iframe renderers never receive api.desktop),
// so a renderer's fallback path covers both without distinguishing them.
//
//     const desktop = useDesktop(api);
//     return desktop
//         ? <button onClick={() => desktop.openExternal(href)}>Open</button>
//         : <a href={href} target="_blank" rel="noreferrer">Open</a>;
//
// Individual capabilities are read off `desktop.capabilities`, which a
// browser tab does not have at all — so test for truthiness, never for
// presence.
// ---------------------------------------------------------------------------

export interface DesktopApi {
    desktop?: DesktopSurface;
}

export function useDesktop(api: DesktopApi | null | undefined): DesktopSurface | undefined {
    return (api && api.desktop) || undefined;
}

// ---------------------------------------------------------------------------
// useVisibility — is this renderer's cell on screen?
//
// Off-screen cells are hidden with display:none, which lets the browser
// drop a canvas/GPU drawing buffer, so a canvas renderer must repaint when
// it becomes visible again:
//
//     const { visible } = useVisibility(api);
//     useEffect(() => { if (visible) repaintMyCanvas(); }, [visible]);
//
// Initialises from the current state; the host fires immediately on
// subscribe. onChange(cb) is the imperative handle (mirrors useTheme).
// Degrades safely: an api without the visibility methods (iframe embed)
// always reports visible=true.
// ---------------------------------------------------------------------------

export interface VisibilityApi {
    getVisible?: () => boolean;
    onVisibleChange?: (cb: (v: boolean) => void) => (() => void) | undefined;
}

export interface VisibilityHandle {
    visible: boolean;
    onChange: (cb: (v: boolean) => void) => (() => void) | undefined;
}

export function useVisibility(api: VisibilityApi | null | undefined): VisibilityHandle {
    const getInitial = (): boolean => (api && api.getVisible ? !!api.getVisible() : true);
    const [visible, setVisible] = useState<boolean>(getInitial);
    useEffect(() => {
        if (!api || !api.onVisibleChange) return undefined;
        return api.onVisibleChange((v) => setVisible(!!v));
    }, [api]);
    const onChange = useCallback(
        (cb: (v: boolean) => void) => {
            if (!api || !api.onVisibleChange) return undefined;
            return api.onVisibleChange((v) => cb(!!v));
        },
        [api],
    );
    return { visible, onChange };
}

// ---------------------------------------------------------------------------
// useHostFocus — does this renderer's cell hold the canvas focus?
//
// Native renderers share the main document, so a window-level input
// listener hears every event app-wide. Any such listener must gate on
// this signal so an unfocused cell never reacts to input meant elsewhere:
//
//     const { focused } = useHostFocus(api);
//     useEffect(() => {
//         const onKey = (e: KeyboardEvent) => { if (!focused) return; ... };
//         window.addEventListener('keydown', onKey);
//         return () => window.removeEventListener('keydown', onKey);
//     }, [focused]);
//
// Mirrors useVisibility. Degrades safely: an api without the focus
// methods (iframe embed, input already scoped to its document) reports
// focused=true.
// ---------------------------------------------------------------------------

export interface HostFocusApi {
    getFocused?: () => boolean;
    onFocusChange?: (cb: (f: boolean) => void) => (() => void) | undefined;
}

export interface HostFocusHandle {
    focused: boolean;
    onChange: (cb: (f: boolean) => void) => (() => void) | undefined;
}

export function useHostFocus(api: HostFocusApi | null | undefined): HostFocusHandle {
    const getInitial = (): boolean => (api && api.getFocused ? !!api.getFocused() : true);
    const [focused, setFocused] = useState<boolean>(getInitial);
    useEffect(() => {
        if (!api || !api.onFocusChange) return undefined;
        return api.onFocusChange((f) => setFocused(!!f));
    }, [api]);
    const onChange = useCallback(
        (cb: (f: boolean) => void) => {
            if (!api || !api.onFocusChange) return undefined;
            return api.onFocusChange((f) => cb(!!f));
        },
        [api],
    );
    return { focused, onChange };
}

// ---------------------------------------------------------------------------
// useAnnotationRequest — answer the host's annotation requests.
//
// The host asks a view for context when a user attaches part of a file to a
// message for a run (`api.onAnnotationRequest`). One handler per view: the
// hook subscribes once per `api` and always calls the latest `handler`, so
// a handler built in render (a useCallback over the view's state) never
// resubscribes. Resolve the context, null when the user abandons a pick, or
// reject with `annotationUnavailable(message)` when the view cannot answer:
//
//     useAnnotationRequest(api, async (request, signal) => {
//         if (request.mode !== 'selection') throw annotationUnavailable('Select some text first.');
//         return { label: `${fileName} › line 3`, text: '…' };
//     });
//
// Inert where the host has no `onAnnotationRequest` (a sealed preview).
// ---------------------------------------------------------------------------

export type AnnotationHandler = (request: AnnotationRequest, signal: AbortSignal) => Promise<AnnotationContext | null>;

export interface AnnotationApi {
    onAnnotationRequest?: (handler: AnnotationHandler) => (() => void) | undefined;
}

/** Subscribe this view to the host's annotation requests once per `api`,
 *  always calling the latest `handler`; null withdraws the subscription (the
 *  view then takes no requests). Does nothing where the host has no
 *  `onAnnotationRequest` (a sealed preview). */
export function useAnnotationRequest(api: AnnotationApi | null | undefined, handler: AnnotationHandler | null): void {
    const latest = useRef(handler);
    latest.current = handler;
    const enabled = !!handler;
    useEffect(() => {
        if (!enabled || !api || typeof api.onAnnotationRequest !== 'function') return undefined;
        return api.onAnnotationRequest((request, signal) => {
            const current = latest.current;
            return current ? current(request, signal) : Promise.resolve(null);
        }) || undefined;
    }, [api, enabled]);
}

/** The rejection that tells the host this view cannot answer the request;
 *  the host shows `message`. */
export function annotationUnavailable(message: string): Error {
    const error = new Error(message);
    error.name = 'AnnotationUnavailable';
    return error;
}

// ---------------------------------------------------------------------------
// useRemoteModule — dynamic ESM import with dedupe, cache, and loading/
// error state. Replaces ad-hoc `ensureMonaco()` / `loadScript()` wrappers.
// ---------------------------------------------------------------------------

const _moduleCache = new Map<string, Promise<unknown>>();

export interface RemoteModuleState<T> {
    module: T | null;
    loading: boolean;
    error: Error | null;
}

export function useRemoteModule<T = unknown>(url: string): RemoteModuleState<T> {
    const [state, setState] = useState<RemoteModuleState<T>>(
        { module: null, loading: true, error: null },
    );
    useEffect(() => {
        let cancelled = false;
        let p = _moduleCache.get(url);
        if (!p) {
            const entry: Promise<unknown> = import(/* @vite-ignore */ /* webpackIgnore: true */ url);
            p = entry;
            _moduleCache.set(url, entry);
            // Evict a failed import so a later mount can retry.
            entry.catch(() => { if (_moduleCache.get(url) === entry) _moduleCache.delete(url); });
        }
        p.then((mod) => {
            if (!cancelled) setState({ module: mod as T, loading: false, error: null });
        }).catch((err: Error) => {
            if (!cancelled) setState({ module: null, loading: false, error: err });
        });
        return () => { cancelled = true; };
    }, [url]);
    return state;
}

// ---------------------------------------------------------------------------
// useRemoteScript — dynamic <script> tag loader with dedupe. For UMD-style
// bundles that expose themselves on `window` (e.g. the monaco loader).
// ---------------------------------------------------------------------------

const _scriptCache = new Map<string, Promise<void>>();

export interface RemoteScriptState {
    loading: boolean;
    error: Error | null;
}

export function useRemoteScript(url: string): RemoteScriptState {
    const [state, setState] = useState<RemoteScriptState>(
        { loading: true, error: null },
    );
    useEffect(() => {
        let cancelled = false;
        let p = _scriptCache.get(url);
        if (!p) {
            p = new Promise<void>((resolve, reject) => {
                if (document.querySelector(`script[src="${url}"]`)) { resolve(); return; }
                const s = document.createElement('script');
                s.src = url;
                s.onload = () => resolve();
                s.onerror = () => { s.remove(); reject(new Error(`Failed to load: ${url}`)); };
                document.head.appendChild(s);
            });
            const entry = p;
            _scriptCache.set(url, entry);
            // Evict a failed load (its tag is removed above) so a later mount can retry.
            entry.catch(() => { if (_scriptCache.get(url) === entry) _scriptCache.delete(url); });
        }
        p.then(() => { if (!cancelled) setState({ loading: false, error: null }); })
         .catch((err: Error) => { if (!cancelled) setState({ loading: false, error: err }); });
        return () => { cancelled = true; };
    }, [url]);
    return state;
}

export { MediaViewer } from './media';
export { StyleControl, StyleControlRow } from './style-control';
export type { StyleControlProps, StyleControlTheme, StyleControlSize, StyleControlExtraContext, StyleControlRowProps } from './style-control';

export {
    DARKREADER_VERSION, DARKREADER_PATH, DARKREADER_MESSAGE,
    HOST_THEME_WAIT_MS, DOCUMENT_VIEW_STYLE,
    darkReaderScriptUrl, darkReaderPoles, darkReaderOptions, injectDarkReader, documentSrcDoc,
    hostThemeKnown, themeSettled, documentSkinKey, documentSkinPost, deliveredAfterLoad,
    frameBacking, useDocumentView,
} from './document-view';
export type {
    DarkReaderOptions, DarkReaderInjection, PreparedDocument, DocumentViewInput, DocumentView, ElementPick,
} from './document-view';
