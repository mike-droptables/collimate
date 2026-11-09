// One strategy for every link a surface wants followed: a renderer's
// `api.openLink` (NativeRenderer, and the iframe hosts' OPEN_LINK message
// through lib/sandboxBridge), the annotation pane's markdown
// (components/MarkdownView) and anything else of the app's own that shows a
// link of a cell. The surface hands over the href as written and the
// document it is written in; this module alone decides what it names and
// what following it does.
//
//   file        a path of the cell: relative to the document's folder (the
//               cell root when there is no document), a leading `/` from the
//               cell root, percent-decoded. Its file opens in a new tab of
//               the cell, or the tab showing it takes focus.
//   local-site  an http(s) URL on this machine (localhost, *.localhost,
//               127.0.0.1, [::1], 0.0.0.0): "View site" asks the host to show
//               the live run of the cell that serves the port
//               (POST …/cells/{cell}/sites); its pointer opens in front.
//   website     any other http(s) URL, a URL on the app's own origin, and
//               mailto: — "Open in browser" or "Copy URL".
//   outside     a path that climbs out of the cell: refused.
//   unsupported every other scheme (file:, javascript:, custom ones).

import { getApiUrl } from '@/lib/api/client';
import { postCellSite } from '@/lib/api/runs';
import { copyText } from '@/lib/clipboard';
import { currentChannelId } from '@/lib/currentChannel';
import { isDesktop, openExternal } from '@/lib/desktop';
import { revealCellFile } from '@/lib/revealFile';
import { useCollimateStore } from '@/stores/useCollimateStore';

export type LinkTarget =
    | { kind: 'file'; path: string }
    | { kind: 'local-site'; url: string; port: number }
    | { kind: 'website'; url: string }
    | { kind: 'outside' }
    | { kind: 'unsupported' };

export interface LinkContext {
    /** The cell the link belongs to. */
    cellId: string;
    /** The cell-relative path of the document the link is written in; a
     *  relative path resolves against its folder. Absent: the cell root. */
    documentPath?: string;
    /** The cell's channel; the active channel when absent. */
    channel?: string;
}

const SCHEME = /^([a-z][a-z0-9+.-]*):/i;
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '0.0.0.0']);

/** The origins the app itself is served from: the page's and the API's.
 *  A URL there is the app's own page, never a site a run serves. */
function appOrigins(): Set<string> {
    const origins = new Set<string>();
    if (typeof window === 'undefined') return origins;
    origins.add(window.location.origin);
    try { origins.add(new URL(getApiUrl(), window.location.href).origin); } catch { /* an unparsable API base names no origin */ }
    return origins;
}

function isLoopbackHost(hostname: string): boolean {
    const host = hostname.toLowerCase();
    return LOOPBACK_HOSTS.has(host) || host.endsWith('.localhost');
}

/** The cell path `ref` (scheme-less, fragment and query removed) names from
 *  the document at `documentPath`, or null when it climbs out of the cell.
 *  Each segment is percent-decoded before it is read, so an encoded `..` or
 *  separator climbs and splits like a literal one. */
function resolveCellPath(ref: string, documentPath: string | undefined): string | null {
    const doc = documentPath ?? '';
    const folder = doc.includes('/') ? doc.slice(0, doc.lastIndexOf('/') + 1) : '';
    const joined = ref.startsWith('/') || ref.startsWith('\\') ? ref : folder + ref;
    const out: string[] = [];
    for (const raw of joined.split(/[/\\]/)) {
        let decoded: string;
        try { decoded = decodeURIComponent(raw); } catch { decoded = raw; }
        for (const segment of decoded.split(/[/\\]/)) {
            if (segment === '' || segment === '.') continue;
            if (segment === '..') {
                if (!out.length) return null;
                out.pop();
                continue;
            }
            out.push(segment);
        }
    }
    return out.join('/');
}

function classifyUrl(href: string): LinkTarget {
    let url: URL;
    try { url = new URL(href, typeof window === 'undefined' ? undefined : window.location.href); } catch { return { kind: 'unsupported' }; }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return { kind: 'unsupported' };
    if (!isLoopbackHost(url.hostname) || appOrigins().has(url.origin)) return { kind: 'website', url: url.href };
    const port = url.port ? Number(url.port) : url.protocol === 'https:' ? 443 : 80;
    return { kind: 'local-site', url: url.href, port };
}

/** What `href`, written in `ctx.documentPath` of the cell, names. */
export function classifyLink(href: string, ctx: Pick<LinkContext, 'documentPath'>): LinkTarget {
    const ref = href.trim();
    if (ref.startsWith('//')) return classifyUrl(ref);
    const scheme = SCHEME.exec(ref)?.[1].toLowerCase();
    if (scheme === 'http' || scheme === 'https') return classifyUrl(ref);
    if (scheme === 'mailto') return { kind: 'website', url: ref };
    if (scheme !== undefined || ref.startsWith('#')) return { kind: 'unsupported' };
    const bare = ref.split(/[?#]/)[0];
    // An empty reference names the document itself, as a browser reads it.
    if (!bare) return { kind: 'file', path: ctx.documentPath ?? '' };
    const path = resolveCellPath(bare, ctx.documentPath);
    return path === null ? { kind: 'outside' } : { kind: 'file', path };
}

/** Open `url` outside the app: the desktop shell hands it to the system
 *  (http(s) and mailto: only), a browser tab opens it in a new tab. */
export function openOutside(url: string): void {
    if (isDesktop()) void openExternal(url);
    else window.open(url, '_blank', 'noopener');
}

async function alertIn(cellId: string, title: string, body: string): Promise<void> {
    await useCollimateStore.getState().openAlert({ title, body, ...(cellId ? { cellId } : {}) });
}

/** Follow `href` as the user's click on it asks: open the cell's file,
 *  offer to view a local site or to open or copy a web address, or say why
 *  the link cannot be followed. Every dialog shows in the link's cell. */
export async function openLink(href: string, ctx: LinkContext): Promise<void> {
    const { cellId } = ctx;
    const target = classifyLink(href, ctx);
    const store = useCollimateStore.getState();
    switch (target.kind) {
        case 'file': {
            const files = cellId ? store.locateCell(cellId)?.cell.files ?? [] : [];
            if (!target.path || !files.some(file => file.name === target.path)) {
                await alertIn(cellId, 'File not found', `“${target.path || href.trim()}” is not a file of this cell.`);
                return;
            }
            revealCellFile(cellId, target.path, { newTab: true });
            return;
        }
        case 'local-site': {
            if (!cellId) {
                await alertIn(cellId, 'Cannot view site', 'A site on this machine can be viewed only from a link in a cell.');
                return;
            }
            const choice = await store.openChoice({
                title: target.url,
                body: `A site on this machine, port ${target.port}. View site shows it in a tab of this cell, from the running runner of the cell that serves the port. The page runs with this app's access, as a runner's own preview does: view only sites you trust.`,
                options: [{ id: 'view', label: 'View site' }],
                cellId,
            });
            if (choice !== 'view') return;
            let site;
            try {
                site = await postCellSite(ctx.channel || currentChannelId(), cellId, target.port);
            } catch (error) {
                await alertIn(cellId, 'Cannot view site', error instanceof Error ? error.message : String(error));
                return;
            }
            // The host's broadcast reveals the pointer to every viewer under
            // the run-reveal rule (behind a tab of another run); the viewer
            // who asked sees it in front.
            store.queueReveal(cellId, { path: site.path, runId: site.run_id, activate: true });
            return;
        }
        case 'website': {
            const choice = await store.openChoice({
                title: target.url,
                options: [{ id: 'open', label: 'Open in browser' }, { id: 'copy', label: 'Copy URL' }],
                ...(cellId ? { cellId } : {}),
            });
            if (choice === 'open') openOutside(target.url);
            else if (choice === 'copy') await copyText(target.url, { cellId });
            return;
        }
        case 'outside':
            await alertIn(cellId, 'Cannot open link', `“${href.trim()}” points outside this cell.`);
            return;
        case 'unsupported':
            await alertIn(cellId, 'Cannot open link', 'Links of this kind cannot be opened here.');
            return;
    }
}
