// Import and export over HTTP (api/collimate_server/routes/transfer.py). Uploads send a File
// or Blob as the raw request body: the browser streams it from disk, so a
// large file never sits in page memory, and the server hashes it into the
// CAS as it arrives. Exports come back as a manifest of CAS hashes (a
// folder export fetches each one through GET /api/channels/{channel}/blob/{hash}) or as one
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

/** Stream one File or Blob into the channel's body store
 *  (POST /api/channels/{channel}/blob/stream). */
export const streamBlob = (channel: string, body: Blob, options: UploadOptions = {}): Promise<StoredBlob> =>
    postBody<StoredBlob>(`${channelPath(channel)}/blob/stream`, body, options);

export interface ZipEntry {
    path: string;
    blob_hash: string;
    size: number;
    encoding: BlobEncoding;
}

/** An uploaded repository's `.git`, held by the server under `token`
 *  until an import commit adopts it: `dir` is the folder it sat in,
 *  relative to the upload's root (`''` for its root). */
export interface UploadedRepository {
    dir: string;
    token: string;
}

/** What the server found in an uploaded zip: its files (already in the
 *  CAS), its empty folders, each `.git` it set aside as a repository, and
 *  the members it refused with why. */
export interface ZipExpansion {
    entries: ZipEntry[];
    dirs: string[];
    repositories: UploadedRepository[];
    skipped: { path: string; reason: string }[];
}

/** Upload a zip and have the server expand it into an existing channel's
 *  body store (POST /api/channels/{channel}/import/zip). Nothing is created
 *  yet: an import commit references the hashes. */
export const expandZip = (channel: string, zip: Blob, options: UploadOptions = {}): Promise<ZipExpansion> =>
    postBody<ZipExpansion>(`${channelPath(channel)}/import/zip`, zip, options);

/** The same expansion for the new-channel page, whose channel does not exist
 *  yet (POST /api/import/zip): the server stages the bodies and copies them
 *  into the channel when it is created. */
export const expandZipForNewChannel = (zip: Blob, options: UploadOptions = {}): Promise<ZipExpansion> =>
    postBody<ZipExpansion>('/import/zip', zip, options);

/** Upload one repository's `.git` tree, zipped with its members under
 *  `<dir>/.git/` (POST /api/channels/{channel}/import/repository, multipart
 *  field `file`). */
export async function uploadRepository(channel: string, zip: Blob, { signal, onProgress }: UploadOptions = {}): Promise<UploadedRepository> {
    const form = new FormData();
    form.append('file', zip, 'repository.zip');
    const response = await api.post<UploadedRepository>(`${channelPath(channel)}/import/repository`, form, {
        signal,
        onUploadProgress: onProgress ? event => onProgress(event.loaded, event.total ?? zip.size) : undefined,
    });
    return response.data;
}

export interface ImportFile {
    path: string;
    blob_hash: string;
}

/** A repository a new cell adopts: its folder in the cell (`''` = the
 *  root) and the token of its uploaded `.git`. */
export interface ImportRepository {
    repo_path: string;
    token: string;
}

export interface ImportCell {
    name: string;
    files: ImportFile[];
    /** Empty folders, each kept by a `.collkeep` the server mints. */
    dirs: string[];
    repositories?: ImportRepository[];
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
    /** What the cells were made without: a repository's `.git` git would
     *  not adopt (`{path: "<repo_path>/.git", reason}`); its files came in. */
    skipped?: { path: string; reason: string }[];
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

/** A cell file of a folder export, fetched from the CAS by its hash. */
export interface ExportBlobEntry {
    path: string;
    body_hash: string;
    size: number;
    encoding: BlobEncoding;
}

/** A file the export staged itself (a repository's `.git`: its packs,
 *  refs and index), fetched through the export's token by `export_file`,
 *  its name inside the staging. */
export interface ExportStagedEntry {
    path: string;
    export_file: string;
    size: number;
}

export type ExportEntry = ExportBlobEntry | ExportStagedEntry;

/** The tree an export writes: every file's path under `root` (a cell file
 *  by its CAS hash, a staged one by its export file), and the folders that
 *  hold no file. `token` names the staging that serves the staged files
 *  until the export is released; `notes` say what the export left behind
 *  (a repository's stash with its local history omitted). */
export interface ExportManifest {
    root: string;
    entries: ExportEntry[];
    dirs: string[];
    token?: string;
    notes?: string[];
}

/** How an export treats each repository's history: with `omitGitHistory`
 *  ("Omit local git history (reflogs, stash)") its `.git` keeps the refs
 *  and an operation under way, without reflogs or the stash. */
export interface ExportChoices {
    omitGitHistory?: boolean;
}

function exportQuery(target: ExportTarget, choices: ExportChoices = {}): string {
    const params = new URLSearchParams({ scope: target.scope });
    if (target.scope === 'cell') params.set('cell_id', target.cellId);
    if (target.scope === 'stack') params.set('stack_id', target.stackId);
    if (target.scope === 'tag') params.set('tag', target.tag);
    if (choices.omitGitHistory) params.set('omit_git_history', 'true');
    return params.toString();
}

/** GET /api/channels/{channel}/export/manifest. */
export const fetchExportManifest = (channel: string, target: ExportTarget, signal?: AbortSignal, choices: ExportChoices = {}): Promise<ExportManifest> =>
    requestJson<ExportManifest>(`${channelPath(channel)}/export/manifest?${exportQuery(target, choices)}`, 'GET', undefined, { signal });

/** A staged file of a folder export (GET …/export/{token}/file). */
export const exportFileUrl = (channel: string, token: string, exportFile: string): string =>
    `${getApiUrl()}${channelPath(channel)}/export/${encodeURIComponent(token)}/file?${new URLSearchParams({ path: exportFile })}`;

/** Release a folder export's staging once its files are written (DELETE
 *  …/export/{token}); the host sweeps one left behind after an hour. */
export const releaseExport = async (channel: string, token: string): Promise<void> => {
    await requestJson(`${channelPath(channel)}/export/${encodeURIComponent(token)}`, 'DELETE');
};

/** The streamed zip of an export: a download link's href, or a fetch whose
 *  body is piped into a file the user picked. */
export const exportZipUrl = (channel: string, target: ExportTarget, choices: ExportChoices = {}): string =>
    `${getApiUrl()}${channelPath(channel)}/export.zip?${exportQuery(target, choices)}`;

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
