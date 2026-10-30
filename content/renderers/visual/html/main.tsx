/**
 * HTML — the generic page renderer (binds `.html` / `.htm`).
 *
 * PAGE MODE by default (agent-v2 §5.3): the file renders full-width as the
 * document it is — no code pane (editing is "Open with → code editor").
 * `live_refresh: true` in the manifest keeps the host from remounting on
 * mid-run writes; the srcDoc simply swaps in place as the content updates.
 *
 * In-cell sibling references (`<img src="chart.png">`, a relative
 * stylesheet, `<a href="notes.md">`) are read as a browser reads them beside
 * the page: relative to the page's own folder (`../` climbs, within the
 * cell), a leading `/` meaning the cell root (links.ts `resolveCellPath`).
 * They are rewritten to ABSOLUTIZED cell-file URLs
 * (`api.getFileUrl`) — binary-safe, accepted-current-revision, so an asset the generator
 * writes mid-run shows immediately. Never the old `getSiblingFile` →
 * `Blob` → `createObjectURL` loop: `getSiblingFile` decodes bytes as text
 * (corrupting every image), and a blob URL is keyed to the minting realm —
 * the sandboxed preview is a different origin, so blob images render
 * broken. Absolutize deliberately: `getFileUrl` can be relative
 * (`../api/…` in the bundled deployment) and `about:srcdoc` resolves
 * relative URLs against the PARENT document's SPA route.
 *
 * The preview sandbox carries NO `allow-same-origin`: scripts in the page
 * run (demos work) in an opaque origin that the server answers only on the
 * cell's capability file URLs `getFileUrl` returns, never on the rest of
 * `/api/*` — any HTML file in a cell may be agent-authored.
 *
 * `supports_diff: true` stays (the SDK's default side-by-side text diff).
 *
 * THE PAGE IS THE AUTHOR'S, shown through the SDK's document view
 * (`useDocumentView`): no frame around it (no margin, no hairline, no
 * radius), no skin colour or typography injected into it. DarkReader,
 * served from the app's own origin, is the only thing that ever recolours
 * the page: in a dark skin by default, darkened onto the skin's colours.
 * The style control (top right, shown only while the pointer is near that
 * corner, so at rest it shows nothing over the page; the page's frame
 * swallows the pointer, so the page reports it, the view's
 * `reportPointer`) chooses the page's look: Auto (darkened in a dark skin,
 * as authored in a light one), As authored, or Darkened, and its text size:
 * a zoom rule at zero specificity (`:where(:root)`, so a page's own zoom
 * wins) that is the view's variables block, rewritten in the loaded page on
 * a change, so a size change never reloads it. The host keeps the choice
 * per channel under this renderer's id. With dark mode off in a dark skin
 * the page sits on the browser's light Canvas, its own default ground. The
 * page fills the renderer's box, in whatever ground it paints. The view
 * waits for the host's skin record before it builds the srcDoc, so the
 * user's page is never loaded twice. What is this renderer's own: the
 * sibling-ref rewrite, the `<base>` and the link bridge below (its prepare
 * step, which takes nothing from the skin) and the frame's sandbox.
 *
 * ANNOTATIONS. The page answers the host's annotation requests
 * (`useAnnotationRequest`) with an element pick: a `mode: 'pick'` request
 * arms the document view's picker (`pickElements`), the user clicks an
 * element of the page (the page's own handlers never see that click, and
 * Esc abandons the pick), and the answer is `elementContext`: a label
 * naming the file and the element's CSS path, markdown with the element's
 * tag, path, id, text and a markup excerpt, and an anchor `{css, id,
 * snippet}`. A `selection` request, and a page whose picker does not answer
 * (it navigated away, or a policy refused the script), are refused with
 * `annotationUnavailable`.
 *
 * LINKS. A click on a link to a file of the cell opens that file in a NEW
 * tab of the cell under its own renderer (`api.revealFile(path, { newTab:
 * true })`), so the page's tab stays; a `#anchor` scrolls the page in place;
 * a web URL keeps the browser's behaviour. The page's frame is a separate
 * browsing context the renderer cannot script, so the prepare step puts a
 * small click bridge into the page's head beside the `<base>`, which posts
 * the link to the renderer; links.ts has the mechanism and its limits.
 */
import React, { useCallback, useEffect } from 'https://esm.sh/react@19.3.1?target=es2022';
import {
    defineRenderer, useCollimateFile, useTheme, useStyle, useDocumentView, DOCUMENT_VIEW_STYLE, StyleControl,
    useAnnotationRequest, annotationUnavailable,
} from '/api/sdk/react.js';
import type { AnnotationRequest, AnnotationContext, ElementPick } from '/api/sdk/react.js';
import { OPEN_FILE_MESSAGE, cellPathOfUrl, linkBridge, pageFolderUrl, resolveCellPath } from './links';

export { OPEN_FILE_MESSAGE, cellPathOfUrl, linkBridge, pageFolderUrl, resolveCellPath } from './links';

/** The looks the style control offers; the first (Auto, stored as null)
 *  follows the skin. */
export const PAGE_THEMES = Object.freeze([
    Object.freeze({ id: 'skin', label: 'Auto' }),
    Object.freeze({ id: 'authored', label: 'As authored' }),
    Object.freeze({ id: 'darkened', label: 'Darkened' }),
]);

/** The page's text size range, as a factor of the page's own (1). */
export const PAGE_TEXT_SIZE = Object.freeze({ min: 0.5, max: 2, step: 0.1, default: 1 });

/** The document view's `dark` for a stored look: Darkened true, As authored
 *  false, anything else (Auto, null) the skin's scheme. */
export function pageDark(themeId: unknown): boolean | null {
    if (themeId === 'darkened') return true;
    if (themeId === 'authored') return false;
    return null;
}

/** The text size as a rule for the variables block: the whole page zoomed,
 *  at zero specificity (a page's own zoom wins); empty at the page's own
 *  size. */
export function pageZoomRule(textSize: unknown): string {
    if (typeof textSize !== 'number' || !Number.isFinite(textSize)) return '';
    const factor = Math.min(PAGE_TEXT_SIZE.max, Math.max(PAGE_TEXT_SIZE.min, Math.round(textSize * 100) / 100));
    return factor === PAGE_TEXT_SIZE.default ? '' : `:where(:root) { zoom: ${factor}; }`;
}

/** The absolute URL of a file of the cell, or of the cell's file base for
 *  `''`. A relative `getFileUrl` (`../api/…` in the bundled deployment) is
 *  absolutized against the app: `about:srcdoc` would resolve it against the
 *  PARENT document's SPA route. */
function cellFileUrl(path: string, api: any): string {
    return new URL(api.getFileUrl(path), window.location.href).href;
}

/** The URL a reference to the cell file `path` is rewritten to: the host's
 *  revision-pinned URL, or the plain one under the cell's file base for a
 *  path the host does not list (yet). */
function pageRefUrl(path: string, api: any, cellBase: string): string {
    try {
        return cellFileUrl(path, api);
    } catch {
        return cellBase + path.split('/').map(encodeURIComponent).join('/');
    }
}

function rewriteSiblingRefs(html: string, api: any, docPath: string): string {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    const cellBase = cellFileUrl('', api);
    for (const el of Array.from(doc.querySelectorAll('[src], [href]'))) {
        for (const name of ['src', 'href']) {
            const url = el.getAttribute(name);
            const path = url == null ? null : resolveCellPath(docPath, url);
            if (path !== null) el.setAttribute(name, pageRefUrl(path, api, cellBase));
        }
    }
    // Forms such as CSS url(), srcset and script-created relative URLs do not
    // pass through the attribute walk. Their document base is the page's
    // folder under the cell's capability directory, never the containing
    // app document's route. The link bridge follows it.
    // The bridge follows the `<base>`, both ahead of the page's own head.
    const bridge = doc.createElement('script');
    bridge.textContent = linkBridge(cellBase);
    doc.head.prepend(bridge);
    const base = doc.createElement('base');
    base.href = pageFolderUrl(cellBase, docPath);
    doc.head.prepend(base);
    // Emit a doctype so the preview renders in standards mode, not quirks.
    return '<!doctype html>\n' + doc.documentElement.outerHTML;
}

/** The html renderer's prepare step for the document view: the sibling
 *  refs, the `<base>` and the link bridge. */
function preparePage(html: string, api: any, docPath: string) {
    return { html: rewriteSiblingRefs(html, api, docPath) };
}

// ── annotations: an element of the page ───────────────────────────────────

/** The longest label an element pick gives (the host shows it as a chip). */
export const PICK_LABEL_MAX = 80;

/** `value` as markdown inline code: a backtick fence one longer than the
 *  longest run inside, padded with a space when the value starts or ends
 *  with a backtick. */
export function inlineCode(value: string): string {
    const runs = value.match(/`+/g) || [];
    const fence = '`'.repeat(Math.max(0, ...runs.map((run) => run.length)) + 1);
    const pad = value.startsWith('`') || value.endsWith('`') ? ' ' : '';
    return fence + pad + value + pad + fence;
}

/** A code fence for `value`: three backticks, or one longer than the
 *  longest run of three or more inside. */
export function fenceFor(value: string): string {
    const runs = value.match(/`{3,}/g) || [];
    return '`'.repeat(Math.max(2, ...runs.map((run) => run.length)) + 1);
}

/** The chip for an element of `fileName`: the file's name (at most 40
 *  characters) and the element's CSS path, the path's tail kept when the
 *  whole is over `PICK_LABEL_MAX`. */
export function pickLabel(fileName: string, css: string): string {
    const base = fileName.split('/').pop() || fileName;
    const name = base.length > 40 ? base.slice(0, 39) + '…' : base;
    const full = `${name} › ${css}`;
    if (full.length <= PICK_LABEL_MAX) return full;
    return `${name} › …${css.slice(css.length - (PICK_LABEL_MAX - name.length - 4))}`;
}

/** The markdown a run reads for an element of `fileName`: its tag, CSS
 *  path and id, then its text as a quote and its markup in a fence. */
export function pickText(fileName: string, pick: ElementPick): string {
    const where = `css ${inlineCode(pick.css || pick.tag)}` + (pick.id ? `, id ${inlineCode(pick.id)}` : '');
    const lines = [`Element ${inlineCode(`<${pick.tag}>`)} in ${inlineCode(fileName)} (${where}):`];
    if (pick.snippet) lines.push('', `> ${pick.snippet}`);
    if (pick.html) {
        const fence = fenceFor(pick.html);
        lines.push('', `${fence}html`, pick.html, fence);
    }
    return lines.join('\n');
}

/** The annotation context for an element picked in `fileName`. */
export function elementContext(fileName: string, pick: ElementPick): AnnotationContext {
    return {
        label: pickLabel(fileName, pick.css || pick.tag),
        text: pickText(fileName, pick),
        anchor: { css: pick.css, id: pick.id, snippet: pick.snippet },
    };
}

function HtmlPage() {
    const { content, api, fileName } = useCollimateFile();
    const theme = useTheme(api);
    const style = useStyle(api);
    // The text size's zoom is the view's block: a size change rewrites it in
    // the loaded page, no reload.
    const variables = pageZoomRule(style.textSize);
    const prepare = useCallback((html: string) => preparePage(html, api, fileName), [api, fileName]);
    // The style control shows while the pointer is near its corner. The
    // page's frame takes every move over it (this document never sees one),
    // so the page reports the pointer (`reportPointer`) and the control
    // measures those reports over its `frame`: nothing covers the page.
    const view = useDocumentView({
        html: content, api, theme, dark: pageDark(style.theme), prepare, variables, reportPointer: true,
        pickElements: true,
    });
    // The host's annotation requests: a pick arms the page's picker, and the
    // element clicked is the answer. Esc in the page abandons it (null).
    const pick = view.pick;
    const answer = useCallback(async (request: AnnotationRequest, signal: AbortSignal): Promise<AnnotationContext | null> => {
        if (request.mode !== 'pick') throw annotationUnavailable('This page answers element picks only.');
        let picked: ElementPick | null;
        try { picked = await pick(signal, request.hint); }
        catch (error) { throw annotationUnavailable(error instanceof Error ? error.message : String(error)); }
        return picked ? elementContext(fileName, picked) : null;
    }, [pick, fileName]);
    useAnnotationRequest(api, answer);
    // The page's link bridge posts a click on a link to a file of the cell;
    // only the page's own window is heard, and the cell path is the
    // renderer's reading of the address, never the page's word for it.
    useEffect(() => {
        const onMessage = (event: MessageEvent) => {
            const frame = view.frameRef.current;
            if (!frame || event.source !== frame.contentWindow) return;
            const data = event.data;
            if (!data || data.type !== OPEN_FILE_MESSAGE || typeof data.href !== 'string') return;
            const path = cellPathOfUrl(data.href, cellFileUrl('', api));
            if (path) api.revealFile(path, { newTab: true });
        };
        window.addEventListener('message', onMessage);
        return () => window.removeEventListener('message', onMessage);
    }, [api, view.frameRef]);
    return (
        <div style={view.pageStyle}>
            {view.srcDoc !== null && (
                <iframe
                    ref={view.frameRef}
                    title="page"
                    srcDoc={view.srcDoc}
                    onLoad={view.onLoad}
                    style={DOCUMENT_VIEW_STYLE.iframe}
                    sandbox="allow-scripts allow-forms allow-popups"
                />
            )}
            <StyleControl api={api} themes={PAGE_THEMES} font={false} size={PAGE_TEXT_SIZE} frame={view.frameRef} />
        </div>
    );
}

export default defineRenderer({ component: HtmlPage });
