import { fileFetchCache } from '@/lib/fileFetchCache';
import { hydrateFiles } from '@/lib/fileCommands';
// Data layer for the files-pane history mode (the run-node rail).
//
// Fetches GET /cells/{id}/history while a cell is in history mode, resolves
// the slice's identity-keyed selection ({nodeId, versionId}) against the
// fetched nodes, and loads the selected node's committed file tree plus the
// previous node's contents (the A/M/D badge + diff base). Content caches are
// keyed by immutable ids (version cell id / blob hash), so retained values
// can be reused across scrubbing; accelerator eviction may require a refetch.
//
// Refresh: on cell.updated_at changes (debounced) — a live run's emits and
// idle checkpoints both bump it. Selection never moves on refetch: node ids
// are stable, and a vanished id falls back to the current node.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
    fetchBlobText,
    fetchCellFilesTree,
    fetchCellHistory,
    HistoryNode,
    HistoryResponse,
} from '@/lib/api';
import { FileBody } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';

// The shared cache owns values, single-flight requests and retry backoff.
// Version IDs/blob hashes address immutable resources; live diffs include stamps.
const RETRY_MS = 3000;
/** Re-render hooks of every instance in history mode when a shared fetch
 *  settles: a second instance that asked for an in-flight key (a remount, or
 *  two cells showing the same blob) skipped firing and must still hear when
 *  the data lands. */
const listeners = new Set<() => void>();
const notifyAll = () => { for (const fn of listeners) fn(); };
const shouldFire = fileFetchCache.canFetch;

// Selection-local immutable-body cache. Reads adopt warm accelerator hits so
// global pressure cannot evict a mounted view's active working set.
class HistoryResources {
    constructor(readonly scope: {
        channel: string | null; cellId: string; active: boolean;
        selVersionId: string | null; baseVersionId: string | null; nodeId: string | undefined;
    }) {}
    readonly trees = new Map<string, FileBody[]>();
    readonly blobs = new Map<string, string>();

    blobText(hash: string): string | undefined {
        const body = this.blobs.get(hash) ?? fileFetchCache.get<string>(`blob:${hash}`);
        if (body !== undefined) this.blobs.set(hash, body);
        return body;
    }
}

export interface CellHistoryView {
    history: HistoryResponse | null;
    loading: boolean;
    error: string | null;
    /** The effective selected node (slice nodeId, else the current node). */
    selNode: HistoryNode | null;
    /** The effective version within the node: slice versionId when nested,
     *  else the node's last version; null for a zero-version run node. */
    selVersionId: string | null;
    /** The selected node's committed files (its end state, or the nested
     *  version's). [] for a zero-version node; null while loading. */
    selFiles: FileBody[] | null;
    /** Previous (older) node's end-state contents — the badge/diff base.
     *  null while loading; {} when there is no older node. */
    baseFiles: Record<string, string> | null;
    /** Blob text by hash — returns undefined until fetched (triggers the
     *  fetch; re-renders when it lands). */
    artifactText: (blobHash: string) => string | undefined;
}

export function useCellHistory(
    channel: string | null,
    cellId: string,
    active: boolean,
    cellUpdatedAt: string | undefined,
): CellHistoryView {
    const slice = useCollimateStore(s => s.cellHistory[cellId]);
    const [history, setHistory] = useState<HistoryResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [, setContentNonce] = useState(0);
    const bump = useCallback(() => setContentNonce(n => n + 1), []);
    useEffect(() => {
        if (!active) return;
        listeners.add(bump);
        return () => { listeners.delete(bump); };
    }, [active, bump]);

    const fetchNonceRef = useRef(0);
    const refetch = useCallback(() => {
        if (!channel || !active) return;
        const nonce = ++fetchNonceRef.current;
        fetchCellHistory(channel, cellId)
            .then(resp => {
                if (fetchNonceRef.current !== nonce) return;
                setHistory(resp);
                setError(null);
                setLoading(false);
            })
            .catch((err: unknown) => {
                if (fetchNonceRef.current !== nonce) return;
                setError(String((err as Error).message ?? err));
                setLoading(false);
            });
    }, [channel, cellId, active]);

    // Initial load + debounced refresh on cell settles.
    useEffect(() => {
        if (!active) return;
        setLoading(history === null);
        const t = window.setTimeout(refetch, history === null ? 0 : 400);
        return () => window.clearTimeout(t);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active, refetch, cellUpdatedAt]);

    // Resolve selection by identity, falling back to the current node.
    const selNode = useMemo<HistoryNode | null>(() => {
        if (!history || history.nodes.length === 0) return null;
        const wanted = slice?.nodeId ?? history.current.node_id;
        return history.nodes.find(n => n.node_id === wanted)
            ?? history.nodes.find(n => n.node_id === history.current.node_id)
            ?? history.nodes[0];
    }, [history, slice?.nodeId]);

    const selVersionId = useMemo<string | null>(() => {
        if (!selNode) return null;
        if (slice?.versionId && selNode.versions.some(v => v.cell_id === slice.versionId)) {
            return slice.versionId;
        }
        return selNode.versions.length > 0
            ? selNode.versions[selNode.versions.length - 1].cell_id
            : null;
    }, [selNode, slice?.versionId]);

    // Previous (older) node's end state — the badge/diff base.
    const baseVersionId = useMemo<string | null>(() => {
        if (!history || !selNode) return null;
        const idx = history.nodes.findIndex(n => n.node_id === selNode.node_id);
        for (let i = idx + 1; i < history.nodes.length; i++) {
            const vs = history.nodes[i].versions;
            if (vs.length > 0) return vs[vs.length - 1].cell_id;
        }
        return null;
    }, [history, selNode]);

    // A view owns only its selected/base working set. Changing selection or
    // leaving history drops that owner; in-flight promises can still settle.
    const resources = useMemo(() => new HistoryResources({
        channel, cellId, active, selVersionId, baseVersionId, nodeId: selNode?.node_id,
    }),
        [channel, cellId, active, selVersionId, baseVersionId, selNode?.node_id]);
    const heldTrees = resources.trees;
    const heldBlobs = resources.blobs;

    const loadTree = useCallback((versionId: string | null): FileBody[] | null => {
        if (versionId === null) return [];
        const key = `tree:${channel}:${versionId}`;
        const hit = heldTrees.get(versionId) ?? fileFetchCache.get<FileBody[]>(key);
        if (hit) return hit;
        if (channel && shouldFire(key)) {
            fileFetchCache.fetch(key, async () => {
                const tree = await fetchCellFilesTree(channel, versionId);
                return hydrateFiles(channel, cellId, tree.files, versionId);
            }).then(files => { heldTrees.set(versionId, files); })
                .catch(() => {
                    window.setTimeout(() => { bump(); notifyAll(); }, RETRY_MS + 200);
                }).finally(() => { bump(); notifyAll(); });
        }
        return null;
    }, [channel, cellId, bump, heldTrees]);

    const selFiles = active ? loadTree(selVersionId) : null;
    const baseTree = active && selNode ? loadTree(baseVersionId) : null;
    useEffect(() => {
        if (selVersionId && selFiles) heldTrees.set(selVersionId, selFiles);
        if (baseVersionId && baseTree) heldTrees.set(baseVersionId, baseTree);
    }, [heldTrees, selVersionId, selFiles, baseVersionId, baseTree]);
    const baseFiles = useMemo<Record<string, string> | null>(() => {
        if (baseTree === null) return baseVersionId === null ? {} : null;
        const out: Record<string, string> = {};
        for (const f of baseTree) out[f.name] = f.content ?? '';
        return out;
    }, [baseTree, baseVersionId]);

    const artifactText = useCallback((blobHash: string): string | undefined => {
        const hit = resources.blobText(blobHash);
        if (hit !== undefined) return hit;
        const key = `blob:${blobHash}`;
        if (shouldFire(key)) {
            fileFetchCache.fetch(key, () => fetchBlobText(blobHash))
                .then(text => { heldBlobs.set(blobHash, text); })
                .catch(() => {
                    window.setTimeout(() => { bump(); notifyAll(); }, RETRY_MS + 200);
                })
                .finally(() => { bump(); notifyAll(); });
        }
        return undefined;
    }, [bump, heldBlobs, resources]);

    return {
        history,
        loading,
        error,
        selNode,
        selVersionId,
        selFiles,
        baseFiles,
        artifactText,
    };
}
