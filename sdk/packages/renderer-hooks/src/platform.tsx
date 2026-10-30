// SPDX-License-Identifier: MIT
// Canonical host-mounted renderer primitives; generated runtime imports these.
import React, { createContext, useContext, useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import type { CollimateFile, SiblingFileState, DefineRendererConfig, RendererDefinition, RendererAPI } from './types';

const CollimateContext = createContext<CollimateFile | null>(null);

export function useCollimateFile(): CollimateFile {
    const ctx = useContext(CollimateContext);
    // Null context = the hook was called outside a defineRenderer component —
    // almost always because it was called inside a callback/effect (a
    // Rules-of-Hooks violation) instead of at the component's top level.
    // A named throw beats the opaque "Cannot destructure property 'api' of
    // null" the caller would otherwise hit on the next line.
    if (ctx === null) {
        throw new Error(
            "useCollimateFile() returned no context — call it at the TOP LEVEL " +
            "of your defineRenderer component, not inside a useEffect/callback " +
            "or a nested function."
        );
    }
    return ctx;
}

export function useSiblingFile(path: string): SiblingFileState {
    const ctx = useCollimateFile();
    // The host's direct URL preserves binary bytes and follows this render's path.
    return { url: ctx.api.getFileUrl(path), loading: false };
}

// Mount error boundary: a renderer that throws while rendering (a Rules-of-
// Hooks violation, a bad JSON.parse, a null destructure) would otherwise blank
// the shadow root with only a console error. Show the message in-cell instead,
// so the author sees WHAT broke without opening devtools.
class _RendererErrorBoundary extends React.Component<React.PropsWithChildren, {error: unknown}> {
    constructor(props: React.PropsWithChildren) { super(props); this.state = { error: null }; }
    static getDerivedStateFromError(error: unknown) { return { error }; }
    render() {
        if (this.state.error) {
            const msg = this.state.error instanceof Error ? this.state.error.message : String(this.state.error);
            return React.createElement('div', {
                style: {
                    width: '100%', height: '100%', boxSizing: 'border-box',
                    padding: 16, overflow: 'auto', whiteSpace: 'pre-wrap',
                    fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.55,
                    color: 'var(--red)',
                    background: 'var(--sheet)',
                    borderLeft: '2px solid var(--red)',
                },
            }, 'Renderer crashed at mount:\n\n' + msg);
        }
        return this.props.children;
    }
}

export function defineRenderer(config: DefineRendererConfig): RendererDefinition {
    // Renderers that need to await async work before revealing (crepe/monaco
    // loading CSS + external editor engines) opt in with deferReady: true and
    // call api.ready() themselves when fully painted. Everything else gets a
    // free auto-ready from the useEffect below — fires right after the first
    // React commit, i.e. at the moment the DOM is actually there.
    const deferReady = !!config.deferReady;
    const renderer = {
        mount: async (container: Element, api: RendererAPI) => {
            const content = config.recoverySnapshots
                ? (await api.getDocumentSnapshot()).content
                : await api.getContent();
            const root = createRoot(container);
            const App = () => {
                const [localContent, setLocalContent] = useState(content || '');
                // The host document owner resolves revisions and conflicts.
                // Only its explicit replacement can change the primary body.
                useEffect(() => {
                    return api.onSetContent((value) => setLocalContent(value));
                }, [api]);
                useEffect(() => {
                    if (!deferReady && api.ready) api.ready();
                }, []);
                const saveContent = (newContent: string) => {
                    if (api.isReadOnly()) return;
                    setLocalContent(newContent);
                    api.reportChange(newContent);
                };
                const ctxValue = {
                    initialContent: content || '',
                    content: localContent,
                    saveContent,
                    isReadOnly: api.isReadOnly(),
                    api,
                    fileName: api.getFilename()
                };
                return React.createElement(CollimateContext.Provider, { value: ctxValue },
                    React.createElement(_RendererErrorBoundary, null, React.createElement(config.component)));
            };
            root.render(React.createElement(App));
            return () => root.unmount();
        },
        mountDiff: async (container: Element, api: RendererAPI) => {
            const after = await api.getContent();
            const before = await api.getDiffContent();
            // Names for the two sides (e.g. "v3"/"v4" from the version-
            // history slider); null → diff views fall back to generic
            // "Before"/"After". Diff labels are optional in RendererAPI.
            const labels = api.getDiffLabels ? api.getDiffLabels() : null;
            const root = createRoot(container);
            // Renderers that provide their own diffComponent get full
            // control. Everything else gets a built-in side-by-side text
            // diff — good enough for any text-shaped file without each
            // renderer having to implement one. Before: fake-rendering the
            // "before" content as if it were current silently showed a
            // stale view that looked identical to normal mode.
            const DiffBody = config.diffComponent || _DefaultDiffView;
            // deferReady defers the renderer's own components: a custom
            // diffComponent calls api.ready() itself, but the built-in view
            // is painted at its first commit, so it is ready then (else the
            // host would keep it hidden until its fallback timer).
            const diffDefersReady = deferReady && !!config.diffComponent;
            const App = () => {
                // The host advances the current side; the base stays fixed.
                const [current, setCurrent] = useState(after || '');
                useEffect(() => {
                    return api.onSetContent(setCurrent);
                }, [api]);
                useEffect(() => {
                    if (!diffDefersReady && api.ready) api.ready();
                }, []);
                const ctxValue = {
                    initialContent: after || '',
                    diffContent: before || '',
                    diffLabels: labels,
                    content: current,
                    saveContent: () => {},
                    isReadOnly: true,
                    api,
                    fileName: api.getFilename()
                };
                return React.createElement(CollimateContext.Provider, { value: ctxValue },
                    React.createElement(_RendererErrorBoundary, null, React.createElement(DiffBody)));
            };
            root.render(React.createElement(App));
            return () => root.unmount();
        }
    };
    return renderer;
}

// Built-in default diff — a two-column side-by-side viewer with line-
// level add/remove tinting. Used when a renderer declares supports_diff
// but hasn't implemented its own diffComponent. Deliberately minimal:
// no syntax highlighting, no LCS-reorder detection, just a readable
// before/after pane with changed lines tinted. Every colour is a skin
// token (removed lines on --red / --red-soft, added lines on --green),
// so it follows the host's skin. Renderers that need fancier output
// (monaco side-by-side) override via defineRenderer({ diffComponent }).
function _DefaultDiffView() {
    const { content, diffContent, diffLabels, fileName } = useCollimateFile();
    const before = String(diffContent || '');
    const after = String(content || '');
    // Pane titles: the host's names for the two sides when it has them
    // (version-history scrubbing names them "v3"/"v4"), generic otherwise.
    const beforeTitle = (diffLabels && diffLabels.base) || 'Before';
    const afterTitle = (diffLabels && diffLabels.current) || 'After';
    // Cheap line-by-line compare. Lines present in one side but not the
    // other get tinted; equal lines render as secondary ink. Good enough
    // for a first-pass read; users who need a real diff reach for Monaco
    // or a custom diff.
    const beforeLines = before.split('\n');
    const afterLines = after.split('\n');
    const beforeSet = new Set(beforeLines);
    const afterSet = new Set(afterLines);

    const head: React.CSSProperties = {
        padding: '6px 12px', fontSize: 11, flex: '0 0 auto',
        fontFamily: 'var(--font-mono)', color: 'var(--ink-2)',
        background: 'var(--sheet-2)', borderBottom: '1px solid var(--line)',
    };
    const colHeader: React.CSSProperties = {
        ...head, color: 'var(--ink-3)', textTransform: 'uppercase', letterSpacing: '0.08em',
    };
    const colBody: React.CSSProperties = {
        flex: 1, overflow: 'auto', padding: '8px 0',
        fontFamily: 'var(--font-mono)', fontSize: 12, lineHeight: 1.55,
    };
    const removed = { color: 'var(--red)', background: 'var(--red-soft)' };
    const added = { color: 'var(--green)', background: 'color-mix(in srgb, var(--green) 16%, transparent)' };
    const lineStyle = (tint: { color: string; background: string } | null): React.CSSProperties => ({
        display: 'block', padding: '0 12px', whiteSpace: 'pre-wrap',
        color: tint ? tint.color : 'var(--ink-2)',
        background: tint ? tint.background : 'transparent',
    });

    return React.createElement('div', {
        style: {
            width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
            background: 'var(--sheet)', color: 'var(--ink)',
        },
    },
        React.createElement('div', { style: head }, 'Diff: ' + (fileName || '(untitled)')),
        React.createElement('div', {
            style: { flex: 1, display: 'flex', minHeight: 0 },
        },
            React.createElement('div', {
                style: { flex: 1, display: 'flex', flexDirection: 'column', borderRight: '1px solid var(--line)' },
            },
                React.createElement('div', { style: colHeader }, beforeTitle),
                React.createElement('div', { style: colBody },
                    beforeLines.map((line, i) =>
                        React.createElement('span', { key: 'b' + i, style: lineStyle(afterSet.has(line) ? null : removed) }, line || ' ')
                    )
                ),
            ),
            React.createElement('div', {
                style: { flex: 1, display: 'flex', flexDirection: 'column' },
            },
                React.createElement('div', { style: colHeader }, afterTitle),
                React.createElement('div', { style: colBody },
                    afterLines.map((line, i) =>
                        React.createElement('span', { key: 'a' + i, style: lineStyle(beforeSet.has(line) ? null : added) }, line || ' ')
                    )
                ),
            ),
        ),
    );
}
