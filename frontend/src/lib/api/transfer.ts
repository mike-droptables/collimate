// Import and export over HTTP (api/routes/transfer.py). Uploads send a File
// or Blob as the raw request body: the browser streams it from disk, so a
// large file never sits in page memory, and the server hashes it into the
// CAS as it arrives. Exports come back as a manifest of CAS hashes (a
// folder export fetches each one through GET /api/blob/{hash}) or as one
// streamed zip.
import { api, getApiUrl, requestJson, describeApiError } from './client';

export type BlobEncoding = 'utf-8' | 'binary';

/** A body stored in the CAS. `encoding` is the server's sniff: valid UTF-8
 *  with no NUL byte in its first 8 KiB, at or under 5 MiB, is text. */
export interface StoredBlob {
    blob_hash: string;
    size: number;
    encoding: BlobEncoding;
}

/** Bytes sent so far and the body's size, for one upload. */
export type UploadProgressFn = (sent: number, total: number) => void;

export interface UploadOptions {
    signal?: AbortSignal;
    onProgress?: UploadProgressFn;
}

const channelPath = (channel: string) => `/channels/${encodeURIComponent(channel)}`;

async function postBody<T>(url: string, body: Blob, { signal, onProgress }: UploadOptions): Promise<T> {
    const response = await api.post<T>(url, body, {
        signal,
        headers: { 'Content-Type': body.type || 'application/octet-stream' },
        onUploadProgress: onProgress ? event => onProgress(event.loaded, event.total ?? body.size) : undefined,
    });
    return response.data;
}

/** Stream one File or Blob into the CAS (POST /api/blob/stream). */
export const streamBlob = (body: Blob, options: UploadOptions = {}): Promise<StoredBlob> =>
    postBody<StoredBlob>('/blob/stream', body, options);

export interface ZipEntry {
    path: string;
    blob_hash: string;
    size: number;
    encoding: BlobEncoding;
}

/** What the server found in an uploaded zip: its files (already in the
 *  CAS), its empty folders, and the members it refused with why. */
export interface ZipExpansion {
    entries: ZipEntry[];
    dirs: string[];
    skipped: { path: string; reason: string }[];
}

/** Upload a zip and have the server expand it into the CAS
 *  (POST /api/import/zip). It belongs to no channel and nothing is created
 *  yet: an import commit or a new channel's sources reference the hashes. */
export const expandZip = (zip: Blob, options: UploadOptions = {}): Promise<ZipExpansion> =>
    postBody<ZipExpansion>('/import/zip', zip, options);

export interface ImportFile {
    path: string;
    blob_hash: string;
}

export interface ImportCell {
    name: string;
    files: ImportFile[];
    /** Empty folders, each kept by a `.collkeep` the server mints. */
    dirs: string[];
}

export interface ImportStack {
    name: string;
    cells: ImportCell[];
}

export interface ImportRequest {
    /** The main_stacks index the first new stack takes; null appends. */
    insert_index: number | null;
    stacks: ImportStack[];
}

export interface ImportResult {
    stack_ids: string[];
    cell_ids: string[];
}

/** Create stacks and cells from uploaded blobs, all or nothing
 *  (POST /api/channels/{channel}/import). The caller refetches the state. */
export const commitImport = (channel: string, request: ImportRequest, signal?: AbortSignal): Promise<ImportResult> =>
    requestJson<ImportResult>(`${channelPath(channel)}/import`, 'POST', request, { signal });

/** What an export covers: one cell, one stack, the whole channel, its
 *  Capabilities cells (packages, `packages`), its standard main stacks
 *  (`main`), or every live and removed cell carrying a tag. */
export type ExportTarget =
    | { scope: 'cell'; cellId: string }
    | { scope: 'stack'; stackId: string }
    | { scope: 'all' }
    | { scope: 'packages' }
    | { scope: 'main' }
    | { scope: 'tag'; tag: string };

export interface ExportEntry {
    path: string;
    body_hash: string;
    size: number;
    encoding: BlobEncoding;
}

/** The tree an export writes: every file's path under `root` with its CAS
 *  hash, and the folders that hold no file. */
export interface ExportManifest {
    root: string;
    entries: ExportEntry[];
    dirs: string[];
}

function exportQuery(target: ExportTarget): string {
    const params = new URLSearchParams({ scope: target.scope });
    if (target.scope === 'cell') params.set('cell_id', target.cellId);
    if (target.scope === 'stack') params.set('stack_id', target.stackId);
    if (target.scope === 'tag') params.set('tag', target.tag);
    return params.toString();
}

/** GET /api/channels/{channel}/export/manifest. */
export const fetchExportManifest = (channel: string, target: ExportTarget, signal?: AbortSignal): Promise<ExportManifest> =>
    requestJson<ExportManifest>(`${channelPath(channel)}/export/manifest?${exportQuery(target)}`, 'GET', undefined, { signal });

/** The streamed zip of an export: a download link's href, or a fetch whose
 *  body is piped into a file the user picked. */
export const exportZipUrl = (channel: string, target: ExportTarget): string =>
    `${getApiUrl()}${channelPath(channel)}/export.zip?${exportQuery(target)}`;

/** A failed fetch() response as an Error carrying the server's sentence
 *  (FastAPI's `detail`), the same text the axios client reports. */
export async function responseError(response: Response): Promise<Error> {
    let data: unknown = undefined;
    try { data = await response.json(); } catch { /* a body that is not JSON says nothing more */ }
    const detail = (data as { detail?: unknown } | undefined)?.detail;
    const said = typeof detail === 'string' || Array.isArray(detail);
    const message = said ? describeApiError({ response: { data } }) : `Request failed (${response.status})`;
    return Object.assign(new Error(message), { status: response.status });
}
