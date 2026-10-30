import { getApiUrl } from '@/lib/api';

export interface SandboxSrcParams {
    rendererId: string;
    /** The `hash` query param: a blob: URL whose bytes sandbox.html fetches as
     *  the file content. Empty string = skip the content fetch (renderer
     *  cache-warming preload). */
    fileHash: string;
    documentId?: string;
    fileName: string;
    /** Renderer-facing alias; the filename and all host storage remain real. */
    displayName?: string;
    cellId?: string;
    /** Exact committed snapshot selected in History. */
    versionId?: string;
    /** Channel holding the cell. Lets the sandbox build direct cell-file
     *  URLs synchronously (api.getFileUrl — <img src> etc.); without it the
     *  method still exists but its URLs 404. */
    channel?: string;
    /** Renderer content hash → `rv` cache-bust (optional). */
    rendererHash?: string;
    /** When set, diff mode: adds `diff` + `oldHash` (optional). */
    diffHash?: string;
    /** Names for the diff's two sides (e.g. "v3"/"v4") → `diffBase` /
     *  `diffCurrent` params, surfaced by sandbox.html as
     *  api.getDiffLabels(). Only meaningful alongside diffHash. */
    diffLabels?: { base: string; current: string };
    /** Hard host lock (review cell, diff view) → `readonly=1`, surfaced by
     *  sandbox.html as api.isReadOnly(). Omitted when false so URLs built
     *  before the param existed keep meaning "editable". */
    readOnly?: boolean;
}

/**
 * Build the sandbox.html URL that hosts an iframe-embed renderer.
 *
 * sandbox.html's init() parses these query params (renderer, hash, apiBase,
 * filename, cellId, rv, diff/oldHash, diffBase/diffCurrent, readonly). Keeping
 * the construction in one place
 * gives that contract a single source of truth shared by the pooled
 * {@link SandboxIframe} and the fullscreen {@link StandaloneSandboxIframe} — so
 * a new param can't silently drift across copies.
 *
 * sandbox.html is resolved relative to the current document (/sandbox.html
 * under the FastAPI server).
 */
export function buildSandboxSrc({
    rendererId,
    fileHash,
    documentId,
    fileName,
    displayName,
    cellId,
    versionId,
    channel,
    rendererHash,
    diffHash,
    diffLabels,
    readOnly,
}: SandboxSrcParams): string {
    const apiBase = getApiUrl().replace(/\/api\/?$/, '');
    const sandboxUrl = new URL('sandbox.html', window.location.href).pathname;
    let url = `${sandboxUrl}?renderer=${encodeURIComponent(rendererId)}&hash=${encodeURIComponent(fileHash)}&apiBase=${encodeURIComponent(apiBase)}&filename=${encodeURIComponent(fileName)}&cellId=${encodeURIComponent(cellId || '')}&channel=${encodeURIComponent(channel || '')}`;
    if (documentId) url += `&document=${encodeURIComponent(documentId)}`;
    if (displayName && displayName !== fileName) url += `&displayName=${encodeURIComponent(displayName)}`;
    if (versionId) url += `&versionId=${encodeURIComponent(versionId)}`;
    if (rendererHash) url += `&rv=${encodeURIComponent(rendererHash)}`;
    if (diffHash) url += `&diff=${encodeURIComponent(diffHash)}&oldHash=${encodeURIComponent(diffHash)}`;
    if (diffHash && diffLabels) url += `&diffBase=${encodeURIComponent(diffLabels.base)}&diffCurrent=${encodeURIComponent(diffLabels.current)}`;
    if (readOnly) url += `&readonly=1`;
    return url;
}
