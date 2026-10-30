import type { VirtualFile } from '@/lib/types';
import { api, requestJson, getApiUrl } from './client';


// ── Run-node history (the files-pane history rail) ────────────────────
export interface HistoryVersion {
    cell_id: string;
    index: number;
    created_at: string | null;
    name: string | null;
    file_count: number;
    source_type?: string | null;
    generator_name?: string | null;
    /** Provenance: the generator SOURCE VERSION (a cells-row id on the
     *  generator cell's version chain, checkpointed at run start) that
     *  produced this version. Null for manual edits / imports / pre-
     *  provenance history. */
    generator_cell_id?: string | null;
}

/** A Config/Logs artifact listing entry; content = GET /api/blob/{hash}. */
export interface HistoryArtifact {
    path: string;
    blob_hash: string;
    size: number;
}

export interface HistoryRun {
    /** The runner this run was launched through — the rail's attribution,
     *  falling back to `generator_name` when the run had none. */
    runner_id?: string | null;
    /** That runner's name as the run started under it. */
    runner_name?: string | null;
    run_id: string;
    status: string | null;
    phase: string | null;
    created_at: string | null;
    prompt: string | null;
    generator_name: string | null;
}

/** One rail node: a generator run (config + its versions + logs) or a
 *  maximal consecutive group of system-run versions ("files" node).
 *  `node_id` is stable across refetches — selection keys on it, never on
 *  index, so a head insert can't steal the user's position. */
export interface HistoryNode {
    node_id: string;
    kind: 'run' | 'files';
    run: HistoryRun | null;
    /** Oldest → newest within the node. */
    versions: HistoryVersion[];
    config: HistoryArtifact[];
    logs: HistoryArtifact[];
}

export interface HistoryResponse {
    /** Newest first. */
    nodes: HistoryNode[];
    total_versions: number;
    current: { node_id: string | null; version_cell_id: string };
}

export const fetchCellHistory = async (
    channel: string, visibleCellId: string, signal?: AbortSignal,
): Promise<HistoryResponse> =>
    (await api.get(`/channels/${encodeURIComponent(channel)}/cells/${visibleCellId}/history`, { signal })).data;


/** Raw CAS content URL — immutable-cached, so historical config/log/file
 *  bytes fetch once per hash, ever. Routed through getApiUrl() like every
 *  other URL builder (subpath deployments, the API-settings override). */
export const blobUrl = (blobHash: string): string => `${getApiUrl()}/blob/${blobHash}`;


export const fetchBlobText = async (blobHash: string, signal?: AbortSignal): Promise<string> => {
    const resp = await fetch(blobUrl(blobHash), { signal });
    if (!resp.ok) throw new Error(`blob ${blobHash.slice(0, 8)}: ${resp.status}`);
    return resp.text();
};

export const fetchCellFilesTree = async (
    channel: string, internalCellId: string, signal?: AbortSignal,
): Promise<{ cell_id: string; files: VirtualFile[] }> =>
    (await api.get(`/channels/${encodeURIComponent(channel)}/cells/${internalCellId}/files-tree`, { signal })).data;

export const forkCellVersion = async (
    channel: string, visibleCellId: string, targetCellId: string,
) =>
    requestJson(`/channels/${encodeURIComponent(channel)}/cells/${visibleCellId}/fork`, 'POST', { target_cell_id: targetCellId });


// ── Binary blobs (CAS) ────────────────────────────────────────────────
/** Store bytes already in page memory in the CAS (a renderer's or the
 *  definition viewer's binary save), returning the content hash. Files from
 *  disk stream up through lib/api/transfer.streamBlob instead. */
export const uploadBlob = async (
    bytes: ArrayBuffer | Uint8Array, signal?: AbortSignal,
): Promise<{ blob_hash: string; size: number }> =>
    (await api.post('/blob', bytes, { headers: { 'Content-Type': 'application/octet-stream' }, signal })).data;
