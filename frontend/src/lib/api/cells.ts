import { Channel, Cell, ChannelLayout } from '@/lib/types';
import type { RunOutcome } from '@/generated/channelEvents';
import { requestJson, getApiUrl } from './client';


/** Build a browser-usable URL to a cell file's raw bytes (served from CAS
 *  with the correct Content-Type). Used as the `<img src>` for binary
 *  files — path segments are encoded but slashes preserved. */
export const cellFileUrl = (channel: string, cellId: string, filepath: string, versionId?: string): string => {
    const encPath = filepath.split('/').map(encodeURIComponent).join('/');
    const address = versionId ? `versions/${encodeURIComponent(versionId)}` : `cells/${encodeURIComponent(cellId)}`;
    return `${getApiUrl()}/channels/${encodeURIComponent(channel)}/${address}/files/${encPath}`;
};


export const checkpointCell = async (channel: string, cellId: string) =>
    requestJson(`/channels/${encodeURIComponent(channel)}/cells/${cellId}/checkpoint`, 'POST');

/** Cell fields are edited independently of accepted files. `tags` is the
 *  cell's FULL next list (the server normalizes it; null is refused). */
export interface CellMetaPatch { name?: string; icon?: string | null; pinned?: string | null; settings?: Record<string,unknown> | null; tags?: string[] }
export const patchCellMeta = async (channel:string,cellId:string,meta:CellMetaPatch):Promise<Partial<Cell> & { tags: string[] }> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/meta`,'PATCH',meta);

// ── The overview: the Removed shelf, the journal, the lineage ─────────
// Every time below is UTC ISO 8601 with an offset ('2026-10-06T14:05:00+00:00');
// the client shows it in local time.
const cellsPath = (channel: string, cellId: string) =>
    `/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}`;

/** Who did something: the person at the keyboard (`kind: 'user'`, both
 *  runner fields null) or a runner. */
export interface CellActor {
    kind: 'user' | 'runner';
    runner_id: string | null;
    runner_name: string | null;
}

/** Why a cell is on the shelf: the user removed it, or a Merge or Split
 *  consumed it. */
export type RemovedReason = 'removed' | 'merged' | 'split';
/** `removed` orders by `removed_at`, restamped on every removal. */
export type RemovedSort = 'created' | 'edited' | 'removed';
export type SortDir = 'asc' | 'desc';

/** One removed cell on the shelf (`GET …/cells/removed`). `files` is sorted
 *  and capped at 200 paths, `file_count` counts them all. `preview` is the
 *  head (at most 4 KB) of the pinned file, else the first text file; null
 *  when the cell has none. */
export interface RemovedCell {
    cell_id: string;
    tip_version_id: string | null;
    name: string;
    icon: string | null;
    stack_name: string | null;
    /** The size of the stack it left, counted before the removal. */
    stack_cell_count: number;
    reason: RemovedReason;
    created_at: string | null;
    edited_at: string | null;
    removed_at: string;
    made_by: CellActor;
    files: string[];
    file_count: number;
    preview: { path: string; text: string } | null;
    /** The removed record's tags, in the cell's order. */
    tags: string[];
}

/** The shelf, sorted by the server (ties by cell id). `q`: every term must
 *  match the name, the author, a file name, the preview text or a tag. */
export const fetchRemovedCells = async (
    channel: string, opts: { sort: RemovedSort; dir: SortDir; q?: string },
): Promise<RemovedCell[]> => {
    const params = new URLSearchParams({ sort: opts.sort, dir: opts.dir });
    if (opts.q) params.set('q', opts.q);
    return requestJson(`/channels/${encodeURIComponent(channel)}/cells/removed?${params}`, 'GET');
};

/** Make `tags` the whole tag list of a cell on the shelf (its removed
 *  record, which a restore gives back). 400 for a list the tag rules
 *  refuse, 409 when the cell is on the canvas, 404 without a record. The
 *  receipt carries the normalized list. */
export const putRemovedCellTags = async (channel: string, cellId: string, tags: string[]): Promise<{
    cell_id: string; tags: string[];
}> => requestJson(`/channels/${encodeURIComponent(channel)}/cells/removed/${encodeURIComponent(cellId)}/tags`, 'PUT', { tags });

/** Bring a removed cell back under its own id at the right end of the
 *  board, selected. 409 when it is on the canvas, 404 without a record. */
export const restoreRemovedCell = async (channel: string, cellId: string): Promise<{
    ok: boolean; cell_id: string; stack_id: string; channel: Channel;
}> => requestJson(`${cellsPath(channel, cellId)}/restore`, 'POST');

/** A remove zone's action: tags added to, then taken off, the removed
 *  record (never the live cell). */
export interface RemoveTagAction { add_tags?: string[]; remove_tags?: string[] }

/** Take a canvas cell off the board, onto the shelf, applying `action` to
 *  the removed record's tags (the body is sent only when given). 409 while
 *  it runs, 404 when it is not on the main canvas. Returns the channel
 *  layout. */
export const removeCellById = async (channel: string, cellId: string, action?: RemoveTagAction): Promise<ChannelLayout> =>
    requestJson(`${cellsPath(channel, cellId)}/remove`, 'POST', action);

/** One tag of the channel's vocabulary: how many board cells and removed
 *  records carry it. */
export interface ChannelTag { tag: string; live: number; removed: number }

/** The channel's tag vocabulary (every cell on the board and on the
 *  shelf), sorted casefold. */
export const fetchChannelTags = async (channel: string): Promise<ChannelTag[]> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/tags`, 'GET');

export interface OutlineItem { id: string; name: string }

/** A channel's cells by name, read from its layout: the Capabilities cells
 *  (built-in cards left out), and the standard main stacks with their cells. */
export interface ChannelOutline {
    capabilities_cells: OutlineItem[];
    main_stacks: (OutlineItem & { cells: OutlineItem[] })[];
}

/** GET /api/channels/{channel}/outline. */
export const fetchChannelOutline = async (channel: string): Promise<ChannelOutline> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/outline`, 'GET');

/** Where one admitted package folder of a cell came from. `source_id` is the
 *  literal "zip" for a zip; `package_version` may be null; `sdk_version` is
 *  the SDK the package was admitted under. */
export interface ProvenanceRecord {
    folder: string;
    source_kind: 'bundled' | 'folder' | 'github' | 'zip' | 'channel' | 'cell';
    source_id: string;
    package_kind: string;
    package_id: string;
    package_version: string | null;
    content_hash: string;
    sdk_version: string;
    admitted_at: string;
}

/** A cell's provenance: one record per admitted package folder (none when
 *  the cell has none), and the SDK version the running app uses. */
export interface CellProvenance { sdk_version: string; records: ProvenanceRecord[] }

/** GET /api/channels/{channel}/cells/{cell_id}/provenance. */
export const fetchCellProvenance = async (channel: string, cellId: string): Promise<CellProvenance> =>
    requestJson(`${cellsPath(channel, cellId)}/provenance`, 'GET');

/** Copy a canvas cell into a fresh stack beside `anchorStackId` (else beside
 *  its own stack), applying and selecting it. */
export const duplicateCell = async (channel: string, cellId: string, anchorStackId?: string | null): Promise<{
    ok: boolean; new_stack_id: string; new_cell_id: string; channel: Channel;
}> => requestJson(`${cellsPath(channel, cellId)}/duplicate`, 'POST', { anchor_stack_id: anchorStackId ?? null });

export type JournalKind = 'make' | 'edit' | 'run' | 'duplicate' | 'merge' | 'split' | 'run_cell' | 'cell_renamed';

/** A cell a journal event names: its name and whether it is on the board. */
export interface JournalCellRef {
    cell_id: string;
    name: string;
    status: 'open' | 'removed';
}

/** The run of a `run` or `run_cell` event. `ended_at` is null while it runs;
 *  `note` (one line) and `outcome` come from the generator's finish, else
 *  the host's fallback note. `files_written` is capped at 100 paths. */
export interface JournalRun {
    run_id: string;
    runner_id: string | null;
    runner_name: string | null;
    status: string;
    outcome: RunOutcome | null;
    note: string | null;
    started_at: string | null;
    ended_at: string | null;
    files_written: string[];
}

/** One journal line. `cells` names the cells involved: the cell itself for
 *  make, edit and run; the source then the new cell for run_cell; the
 *  sources then the results for duplicate, merge and split. `files` is
 *  capped at 100 paths, `files_total` counts them all. `version_id` is the
 *  edit's version, or the newest version a run wrote. */
export interface JournalEvent {
    t: string;
    kind: JournalKind;
    old_name?: string;
    new_name?: string;
    by: CellActor;
    cells: JournalCellRef[];
    run: JournalRun | null;
    files: string[];
    files_total: number;
    version_id: string | null;
}

export const JOURNAL_PAGE_LIMIT = 100;

/** A journal page, newest first: the cell's (`cellId`), or every cell's
 *  (`null`, the All cells state). `before` is exclusive; pass the last
 *  event's `t` for the next page, which a page never splits. */
export const fetchJournal = async (
    channel: string, cellId: string | null, page: { before?: string | null; limit?: number } = {},
): Promise<JournalEvent[]> => {
    const params = new URLSearchParams({ limit: String(page.limit ?? JOURNAL_PAGE_LIMIT) });
    if (page.before) params.set('before', page.before);
    const base = cellId ? cellsPath(channel, cellId) : `/channels/${encodeURIComponent(channel)}`;
    return requestJson(`${base}/journal?${params}`, 'GET');
};

export type LineageOp = 'duplicate' | 'merge' | 'split' | 'run_cell';

/** A cell the lineage knows: every cell in the layout (`open`, with its
 *  stack), every removed record and any other cell a move names. */
export interface LineageCell {
    cell_id: string;
    name: string;
    status: 'open' | 'removed';
    stack_id?: string;
}

/** One end of a move: the cell, and its version when the move pinned one. */
export interface LineageEnd {
    cell_id: string;
    version_id: string | null;
}

/** One operation: a duplicate or a run in a new cell (one source, one
 *  result), a merge (its inputs into one cell) or a split (one cell into
 *  its outputs). `runner` is set only on `run_cell`. */
export interface LineageMove {
    id: number;
    op: LineageOp;
    at: string;
    op_id: string;
    from: LineageEnd[];
    to: LineageEnd[];
    runner?: { runner_id: string | null; runner_name: string | null };
}

export interface Lineage {
    cells: LineageCell[];
    moves: LineageMove[];
}

/** The channel's lineage, from the moves record alone (never the files). */
export const fetchLineage = async (channel: string): Promise<Lineage> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/lineage`, 'GET');
