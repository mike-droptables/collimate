// SPDX-License-Identifier: MIT
// ---------------------------------------------------------------------------
// Links in the page: which file of the cell a reference the page loads
// names, and the click bridge that hands every link the user follows to the
// host.
//
// A reference in the page names a file of the cell as the browser would
// read it beside the page: relative to the page's own folder (`../` climbs,
// within the cell), with a leading `/` meaning the cell root. A scheme, a
// protocol-relative `//host`, a pure `#anchor` or a path climbing out of the
// cell names no file of the cell. The renderer rewrites what the page LOADS
// that way (`src`, a stylesheet's `href`); a link (`<a>`, `<area>`) keeps
// the href its author wrote, since following it is the host's to decide.
//
// THE CLICK BRIDGE. The page runs in an opaque srcdoc frame the renderer
// cannot script, so the prepare step puts a few lines of script into its
// head (`linkBridge`), beside the `<base>`. On a primary click that the page
// itself did not handle (`defaultPrevented`), on any link, it cancels the
// navigation and posts the link's href as written to the renderer
// (`OPEN_LINK_MESSAGE`), which hands it to `api.openLink(href, {
// documentPath })` with the page as the document, while a user gesture is
// live (the page's script could post the message itself): the host reads
// it (a file of the cell beside the page, opened in a new tab so the page's
// tab stays; a local site; a web address) and acts. A same-document `#anchor` scrolls
// in place (`location.hash`): the `<base>` would otherwise resolve it
// against the cell's folder and navigate the frame away. A `javascript:`
// link is the page's own script and runs as the page wrote it.
//
// Its limits: the listener sits on the page's window in the bubble phase,
// registered before any script of the page, so a page handler that stops
// the click's propagation keeps it from the bridge (the frame then
// navigates as the browser would), and a page whose own
// Content-Security-Policy refuses inline script runs without it.
// ---------------------------------------------------------------------------

/** The message the page's bridge posts to the renderer for a click on a
 *  link: `{ type, href }`, the link's href as the page wrote it. */
export const OPEN_LINK_MESSAGE = 'collimate:open-link';

/** The cell-relative path an href in the page at `docPath` names, or null
 *  when it is external (a scheme, protocol-relative, a pure anchor) or
 *  escapes the cell root. Relative to the page's own folder; a leading '/'
 *  addresses the cell root. Each segment is decoded before it is classified,
 *  so an encoded `%2e%2e` climbs like `..` and an encoded separator cannot
 *  smuggle a path through one segment. */
export function resolveCellPath(docPath: string, href: string): string | null {
    if (!href || typeof href !== 'string') return null;
    const ref = href.trim();
    if (/^[a-z][a-z0-9+.-]*:/i.test(ref) || ref.startsWith('//') || ref.startsWith('#')) return null;
    const clean = ref.split(/[?#]/)[0];
    if (!clean) return null;
    const doc = docPath || '';
    const baseDir = doc.includes('/') ? doc.slice(0, doc.lastIndexOf('/') + 1) : '';
    const joined = clean.startsWith('/') ? clean.slice(1) : baseDir + clean;
    return cleanSegments(joined.split('/'));
}

/** The decoded, normalized path of `raw` segments, or null when one climbs
 *  out of the root, holds a separator or a NUL, or nothing is left. */
function cleanSegments(raw: string[]): string | null {
    const out: string[] = [];
    for (const part of raw) {
        let seg: string;
        try { seg = decodeURIComponent(part); } catch { seg = part; }
        if (seg.includes('/') || seg.includes('\\') || seg.includes('\u0000')) return null;
        if (seg === '' || seg === '.') continue;
        if (seg === '..') {
            if (out.length === 0) return null;
            out.pop();
            continue;
        }
        out.push(seg);
    }
    return out.length ? out.join('/') : null;
}

/** The URL of the page's folder under the cell's file base, each segment
 *  encoded as the host encodes a file's: the page's `<base>`, so a
 *  reference the attribute walk does not reach (CSS `url()`, `srcset`, a
 *  script's URL) resolves beside the page, as a browser reads it. */
export function pageFolderUrl(cellBase: string, docPath: string): string {
    const doc = docPath || '';
    const dir = doc.includes('/') ? doc.slice(0, doc.lastIndexOf('/')) : '';
    const encoded = dir.split('/').filter(Boolean).map(encodeURIComponent).join('/');
    return encoded ? `${cellBase}${encoded}/` : cellBase;
}

/** The click bridge's script body (see the header). */
export function linkBridge(): string {
    return '(function(){'
        + 'window.addEventListener("click",function(e){'
        + 'if(e.defaultPrevented||e.button!==0)return;'
        + 'var t=e.target,a=t&&t.closest?t.closest("a[href],area[href]"):null;'
        + 'if(!a)return;'
        + 'var raw=(a.getAttribute("href")||"").trim();'
        + 'if(/^javascript:/i.test(raw))return;'
        + 'e.preventDefault();'
        + 'if(raw.charAt(0)==="#"){location.hash=raw;return;}'
        + 'window.parent.postMessage({type:"' + OPEN_LINK_MESSAGE + '",href:raw},"*");'
        + '});})();';
}
