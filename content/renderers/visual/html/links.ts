// SPDX-License-Identifier: MIT
// ---------------------------------------------------------------------------
// Links in the page: which file of the cell a reference names, and the click
// bridge that opens a linked file in a new tab of the cell.
//
// A reference in the page names a file of the cell as the browser would
// read it beside the page: relative to the page's own folder (`../` climbs,
// within the cell), with a leading `/` meaning the cell root. A scheme, a
// protocol-relative `//host`, a pure `#anchor` or a path climbing out of the
// cell names no file of the cell.
//
// THE CLICK BRIDGE. The page runs in an opaque srcdoc frame the renderer
// cannot script, so the prepare step puts a few lines of script into its
// head (`linkBridge`), beside the `<base>`. On a primary click that the page
// itself did not handle (`defaultPrevented`), on a link whose resolved
// address lies under the cell's file base, it cancels the navigation and
// posts the address to the renderer (`OPEN_FILE_MESSAGE`); the renderer
// maps it back to a cell path (`cellPathOfUrl`, the page is not trusted to
// name one) and calls `api.revealFile(path, { newTab: true })`, so the
// page's own tab stays. A same-document `#anchor` scrolls in place
// (`location.hash`): the `<base>` would otherwise resolve it against the
// cell's folder and navigate the frame away. Everything else keeps the
// browser's behaviour: a web URL navigates the frame (a site that refuses
// framing shows the browser's error page) and a `target=_blank` one does
// nothing, since the host's sandbox around an iframe renderer allows no
// popups.
//
// Its limits: the listener sits on the page's window in the bubble phase,
// registered before any script of the page, so a page handler that stops
// the click's propagation keeps it from the bridge (the frame then
// navigates to the file's raw URL, as before the bridge), and a page whose
// own Content-Security-Policy refuses inline script runs without it.
// ---------------------------------------------------------------------------

/** The message the page's bridge posts to the renderer for a click on a
 *  link to a file of the cell: `{ type, href }`, the link's resolved URL. */
export const OPEN_FILE_MESSAGE = 'collimate:open-file';

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

/** The cell path the absolute `url` names under the cell's file base
 *  `cellBase` (`api.getFileUrl('')`, absolutized), or null when it lies
 *  elsewhere: another origin, outside the base, or the base itself. The
 *  query and fragment (a revision pin, an anchor) are not part of it. */
export function cellPathOfUrl(url: string, cellBase: string): string | null {
    let target: URL;
    let base: URL;
    try {
        base = new URL(cellBase);
        target = new URL(url, base);
    } catch {
        return null;
    }
    if (!base.pathname.endsWith('/') || target.origin !== base.origin) return null;
    if (!target.pathname.startsWith(base.pathname)) return null;
    return cleanSegments(target.pathname.slice(base.pathname.length).split('/'));
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

/** The click bridge's script body for a page whose cell files lie under
 *  `cellBase` (see the header). `<` is escaped in the base, so nothing can
 *  close the script element. */
export function linkBridge(cellBase: string): string {
    const base = JSON.stringify(cellBase).replace(/</g, '\\u003c');
    return '(function(){var B=' + base + ';'
        + 'window.addEventListener("click",function(e){'
        + 'if(e.defaultPrevented||e.button!==0)return;'
        + 'var t=e.target,a=t&&t.closest?t.closest("a[href],area[href]"):null;'
        + 'if(!a)return;'
        + 'var raw=(a.getAttribute("href")||"").trim();'
        + 'if(raw.charAt(0)==="#"){e.preventDefault();location.hash=raw;return;}'
        + 'var h=a.href;'
        + 'if(typeof h!=="string"||h.indexOf(B)!==0)return;'
        + 'e.preventDefault();'
        + 'window.parent.postMessage({type:"' + OPEN_FILE_MESSAGE + '",href:h},"*");'
        + '});})();';
}
