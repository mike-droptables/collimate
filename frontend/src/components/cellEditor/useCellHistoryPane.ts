import {textFile} from '@/lib/fileCommands';
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import type { Cell, VirtualFile } from '@/lib/types';
import type { CellHistoryState, CellLayout, HistoryCategory } from '@/stores/collimate/types';
import { DEFAULT_HISTORY_SECTIONS } from '@/stores/collimate/types';
import type { HistoryArtifactCategory } from './HistorySections';
import type { CellFileImportContext } from './useCellFileWrites';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useCellHistory } from '@/hooks/useCellHistory';
import { fetchBlobText, fetchCellHistory, forkCellVersion } from '@/lib/api';
import { activeTab, closeHistoryTabs, openHistoryTab, setFilesPane } from '@/lib/cellLayout';

interface CellHistoryPaneOptions {
    cell: Cell;
    channelId: string | undefined;
    readOnly: boolean;
    cellHistory: CellHistoryState | undefined;
    versionRefreshNonce: number;
    onLayoutChange: (update: (layout: CellLayout) => CellLayout) => void;
    focusedTab: ReturnType<typeof activeTab>;
    fileImportContext: RefObject<CellFileImportContext | null>;
}

/** History stays cell-scoped; delayed artifact copies use the current writer. */
export function useCellHistoryPane({
    cell, channelId, readOnly, cellHistory, versionRefreshNonce,
    onLayoutChange, focusedTab, fileImportContext,
}: CellHistoryPaneOptions) {
    const isHistoryMode = !!cellHistory;
    const openCellHistory = useCollimateStore(s => s.openCellHistory);
    const closeCellHistory = useCollimateStore(s => s.closeCellHistory);
    const setCellHistory = useCollimateStore(s => s.setCellHistory);
    // History availability drives the header button's enabled state: total
    // versions beyond one, OR any run node (a checkpoint-only cell keeps its
    // history; a crashed run's config/log is reachable even with one
    // version). Re-fetch when the cell's updated_at moves OR when a
    // self-initiated checkpoint reports it minted a version — that bump
    // often loses the SSE reconciliation race. Skipped while history mode
    // is on (useCellHistory owns the fetch there).
    const [historyAvailable, setHistoryAvailable] = useState<boolean | null>(null);
    const historyProbe = useRef({ key: '', available: false, dirty: true });
    useEffect(() => { historyProbe.current.dirty = true; }, [cell.updated_at]);
    useEffect(() => {
        const key = `${channelId}:${cell.id}:${versionRefreshNonce}`;
        if (historyProbe.current.key !== key) {
            historyProbe.current = { key, available: false, dirty: true };
            setHistoryAvailable(null);
        }
        if (readOnly || !channelId || isHistoryMode || historyProbe.current.available) return;
        historyProbe.current.dirty = true;
        let cancelled = false;
        let pending = false;
        // Updated-at pushes mark dirty without restarting this interval. A
        // continuous stream therefore cannot postpone the probe indefinitely.
        const timer = setInterval(() => {
            if (pending || !historyProbe.current.dirty || historyProbe.current.available) return;
            pending = true;
            historyProbe.current.dirty = false;
            void fetchCellHistory(channelId, cell.id).then(r => {
                if (cancelled) return;
                const available = r.total_versions > 1 || r.nodes.some(n => n.kind === 'run');
                historyProbe.current.available = available;
                setHistoryAvailable(available);
                if (available) clearInterval(timer);
            }).catch(err => {
                if (!cancelled) console.warn('fetchCellHistory probe failed:', err);
            }).finally(() => { pending = false; });
        }, 500);
        return () => { cancelled = true; clearInterval(timer); };
    }, [cell.id, channelId, readOnly, versionRefreshNonce, isHistoryMode]);

    // ── History mode (the files-pane run-node rail) ────────────────────────
    // The content pane stays LIVE while scrubbing; only the Files
    // pane swaps to the rail + Config/Files/Logs sections. History tabs are
    // read-only views that FOLLOW the scrub; they are swept closed on exit.
    const hv = useCellHistory(channelId ?? null, cell.id, isHistoryMode, cell.updated_at);

    const handleToggleHistory = useCallback(() => {
        if (isHistoryMode) {
            closeCellHistory(cell.id);
            onLayoutChange(l => closeHistoryTabs(l));
        } else {
            openCellHistory(cell.id);
            // A hidden pane would strand an invisible active mode.
            onLayoutChange(l => setFilesPane(l, { collapsed: false }));
        }
    }, [isHistoryMode, cell.id, closeCellHistory, openCellHistory, onLayoutChange]);

    const handleHistorySelect = useCallback((nodeId: string, versionId: string | null) => {
        setCellHistory(cell.id, { nodeId, versionId });
    }, [cell.id, setCellHistory]);

    const handleOpenHistoryEntry = useCallback((category: HistoryCategory, path: string) => {
        onLayoutChange(l => openHistoryTab(l, category, path));
    }, [onLayoutChange]);

    const historyActiveEntry = focusedTab?.kind === 'history'
        ? { category: focusedTab.category, path: focusedTab.path }
        : null;

    // Clone a config/log artifact into the LIVE latest files as a
    // normal editable file. Name collisions auto-suffix (name-2.ext);
    // config/log clones land by basename (their listing paths are
    // artifact-relative, and a 'files/…' folder in the cell would be noise).
    const handleCloneToFiles = useCallback(async (category: HistoryArtifactCategory, path: string) => {
        const sourceChannel = channelId;
        const suffixed = (name: string, files: VirtualFile[]): string => {
            const names = new Set(files.map(f => f.name));
            if (!names.has(name)) return name;
            const dot = name.lastIndexOf('.');
            const stem = dot > 0 ? name.slice(0, dot) : name;
            const ext = dot > 0 ? name.slice(dot) : '';
            for (let i = 2; ; i++) {
                const candidate = `${stem}-${i}${ext}`;
                if (!names.has(candidate)) return candidate;
            }
        };
        let file: VirtualFile | null = null;
        const listing = category === 'config' ? hv.selNode?.config : hv.selNode?.logs;
        const art = listing?.find(a => a.path === path);
        if (art) {
            try {
                const text = await fetchBlobText(art.blob_hash);
                const base = path.split('/').filter(Boolean).pop() ?? path;
                file=textFile(base,text);
            } catch (err) {
                console.warn('history clone failed:', err);
                void useCollimateStore.getState().openAlert({
                    title: 'Copy failed',
                    body: `Could not load ${path}. ${err instanceof Error ? err.message : ''}`,
                });
            }
        }
        // Write through the CURRENT callbacks (the ref is refreshed every
        // render); drop the clone if the cell or channel changed underneath
        // it. The writer adds it to the cell's list as it is at write time —
        // the artifact fetch awaits, and a second clone (or a live run's
        // emit) landing inside that window stays. Names suffix against that
        // list too.
        const target = fileImportContext.current;
        if (!file || !target || target.cellId !== cell.id || target.channelId !== sourceChannel
            || !target.canWrite) return;
        const clone = file;
        target.updateFiles(current => [...current, { ...clone, name: suffixed(clone.name, current) }]);
    }, [cell.id, channelId, hv.selNode, fileImportContext]);

    const handleHistoryBranch = useCallback(async () => {
        if (!channelId || !hv.selVersionId) return;
        try {
            await forkCellVersion(channelId, cell.id, hv.selVersionId);
            closeCellHistory(cell.id);
            onLayoutChange(l => closeHistoryTabs(l));
        } catch (err) {
            console.error('branch from history failed:', err);
            void useCollimateStore.getState().openAlert({
                title: 'Branch failed',
                body: (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
                    || (err instanceof Error ? err.message : 'Could not branch from this version.'),
            });
        }
    }, [channelId, cell.id, hv.selVersionId, closeCellHistory, onLayoutChange]);

    const historyPaneProps = isHistoryMode ? {
        nodes: hv.history?.nodes ?? [],
        selNodeId: hv.selNode?.node_id ?? null,
        selVersionId: hv.selVersionId,
        nested: (cellHistory?.versionId ?? null) !== null,
        onSelect: handleHistorySelect,
        onExit: handleToggleHistory,
        sections: {
            node: hv.selNode,
            loading: hv.loading,
            error: hv.error,
            files: hv.selFiles,
            baseFiles: hv.baseFiles,
            onToggleNested: () => {
                const node = hv.selNode;
                if (!node) return;
                const entering = (cellHistory?.versionId ?? null) === null;
                setCellHistory(cell.id, {
                    nodeId: node.node_id,
                    versionId: entering && node.versions.length > 0
                        ? node.versions[node.versions.length - 1].cell_id
                        : null,
                });
            },
            sectionsOpen: cellHistory?.sectionsOpen ?? DEFAULT_HISTORY_SECTIONS,
            onToggleSection: (section: 'config' | 'files' | 'logs') => {
                const cur = cellHistory?.sectionsOpen ?? DEFAULT_HISTORY_SECTIONS;
                setCellHistory(cell.id, {
                    sectionsOpen: { ...cur, [section]: !cur[section] },
                });
            },
            onOpenEntry: handleOpenHistoryEntry,
            activeEntry: historyActiveEntry,
            onCloneToFiles: readOnly
                ? undefined
                : (category: HistoryArtifactCategory, path: string) => { void handleCloneToFiles(category, path); },
            onBranch: hv.selVersionId && !readOnly ? () => { void handleHistoryBranch(); } : undefined,
        },
    } : null;

    // History-tab bodies resolve content from the CURRENT scrub selection
    // (tabs follow the scrub); mounted under the selected VERSION's cell id —
    // never the live cell's — so every cellId-keyed renderer channel (events,
    // sibling fetches, draft writes) points at the version.
    const historyView = isHistoryMode ? {
        node: hv.selNode,
        versionCellId: hv.selVersionId,
        files: hv.selFiles,
        baseFiles: hv.baseFiles,
        artifactText: hv.artifactText,
    } : null;

    return { historyAvailable, handleToggleHistory, historyPaneProps, historyView };
}
