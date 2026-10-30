import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import type { CellLayout, DockDragState, TabDragState, CellTab } from './types';
import { createDefaultLayout } from '@/lib/cellLayout';
import { encodeCellDisplay, reconcileCellDisplay, type CellDisplay } from '@/lib/cellDisplay';
import { cellDisplayWrites, fetchCellDisplays } from '@/lib/cellDisplayWrites';
import { fetchCellHistory, fetchCellFilesTree } from '@/lib/api/history';
import { requestJson } from '@/lib/api/client';
export interface LayoutSlice {
    cellLayouts: Record<string, CellLayout>;
    cellDisplaysReady: boolean;
    cellDisplayLoadError: string | null;
    cellDisplayErrors: Record<string, string>;
    _cellDisplayEdits: Record<string, number>;
    _cellDisplayFetch: number;
    _cellDisplayValues: Record<string, string>;
    tabDrag: TabDragState | null;
    /** A cell pane's dock icon dragged toward a cell edge
     *  (hooks/useDockDrag). Session-only, like tabDrag. */
    dockDrag: DockDragState | null;
    setCellLayout: (cellId: string, layout: CellLayout) => void;
    seedCellLayout: (cellId: string, layout: CellLayout) => void;
    /** A pure op on the cell's layout; `seed` stands in for a layout the
     *  store does not hold yet (null: nothing to change). */
    updateCellLayout: (cellId: string, fn: (layout: CellLayout) => CellLayout, seed?: () => CellLayout | null) => void;
    /** A pure op on the cell's layout from outside its editor (the header's
     *  status light and caret): a cell the editor has not seeded yet starts
     *  from the default layout, once the cells' displays have loaded. */
    updateRunsPane: (cellId: string, fn: (layout: CellLayout) => CellLayout) => void;
    saveCellDisplay: (cellId: string) => void;
    hydrateCellDisplays: (cellIds?: readonly string[]) => Promise<void>;
    dropCellDisplays: (channel: string, cellIds: readonly string[]) => void;
    retryCellDisplay: (cellId: string) => Promise<void>;
    setTabDrag: (drag: TabDragState | null) => void;
    setDockDrag: (drag: DockDragState | null) => void;
}
/** A located cell's fresh layout (createDefaultLayout over its files and
 *  its pin), or null for a cell the channel does not hold. */
function defaultCellLayout(state: CollimateState, cellId: string): CellLayout | null {
    const loc = state.locateCell(cellId);
    if (!loc)
        return null;
    return createDefaultLayout({
        filePaths: loc.cell.files.map(f => f.name),
        pinnedPath: loc.cell.pinned ?? null,
    });
}
export const createLayoutSlice: StateCreator<CollimateState, [
], [
], LayoutSlice> = (set, get) => ({
    cellLayouts: {}, cellDisplaysReady: false, cellDisplayLoadError: null, cellDisplayErrors: {}, _cellDisplayEdits: {}, _cellDisplayFetch: 0, _cellDisplayValues: {}, tabDrag: null, dockDrag: null,
    seedCellLayout: (cellId, layout) => { if (get().cellDisplaysReady && !get().cellLayouts[cellId])
        set(prev => ({ cellLayouts: { ...prev.cellLayouts, [cellId]: layout }, _cellDisplayValues: { ...prev._cellDisplayValues, [cellId]: JSON.stringify(encodeCellDisplay(layout, prev.cellHistory[cellId])) } })); },
    setCellLayout: (cellId, layout) => { set(prev => ({ cellLayouts: { ...prev.cellLayouts, [cellId]: layout } })); get().saveCellDisplay(cellId); },
    updateCellLayout: (cellId, fn, seed) => {
        const before = get().cellLayouts[cellId] ?? seed?.();
        if (!before)
            return;
        const next = fn(before);
        if (next === get().cellLayouts[cellId])
            return;
        const history = get().cellHistory[cellId];
        const changed = JSON.stringify(encodeCellDisplay(before, history)) !== JSON.stringify(encodeCellDisplay(next, history));
        set(prev => ({ cellLayouts: { ...prev.cellLayouts, [cellId]: next } }));
        if (changed)
            get().saveCellDisplay(cellId);
    },
    // Before the displays load a seeded layout would win over the stored one
    // (hydration skips a cell edited meanwhile), so an unseeded cell waits.
    updateRunsPane: (cellId, fn) => get().updateCellLayout(cellId, fn, () => (get().cellDisplaysReady ? defaultCellLayout(get(), cellId) : null)),
    saveCellDisplay: (cellId) => {
        const state = get(), channel = state.activeChannel?.id;
        if (!channel)
            return;
        let layout = state.cellLayouts[cellId];
        if (!layout) {
            const seeded = defaultCellLayout(state, cellId);
            if (!seeded)
                return;
            layout = seeded;
            set(prev => ({ cellLayouts: { ...prev.cellLayouts, [cellId]: seeded } }));
        }
        const display = encodeCellDisplay(layout, state.cellHistory[cellId]);
        const value = JSON.stringify(display);
        if (state._cellDisplayValues[cellId] === value)
            return;
        set(prev => ({ _cellDisplayEdits: { ...prev._cellDisplayEdits, [cellId]: (prev._cellDisplayEdits[cellId] ?? 0) + 1 }, _cellDisplayValues: { ...prev._cellDisplayValues, [cellId]: value } }));
        cellDisplayWrites.schedule(channel, cellId, display, error => {
            if (get().activeChannel?.id !== channel)
                return;
            set(prev => { const errors = { ...prev.cellDisplayErrors }; if (error)
                errors[cellId] = error;
            else
                delete errors[cellId]; return { cellDisplayErrors: errors }; });
        });
    },
    retryCellDisplay: async (cellId) => { const channel = get().activeChannel?.id; if (channel)
        await cellDisplayWrites.flush(channel, [cellId]); },
    dropCellDisplays: (channel, cellIds) => {
        cellDisplayWrites.forget(channel, cellIds);
        if (get().activeChannel?.id !== channel)
            return;
        set(prev => { const layouts = { ...prev.cellLayouts }, history = { ...prev.cellHistory }, errors = { ...prev.cellDisplayErrors }, edits = { ...prev._cellDisplayEdits }, values = { ...prev._cellDisplayValues }; for (const id of cellIds) {
            delete layouts[id];
            delete history[id];
            delete errors[id];
            delete values[id];
            edits[id] = (edits[id] ?? 0) + 1;
        } return { cellLayouts: layouts, cellHistory: history, cellDisplayErrors: errors, _cellDisplayEdits: edits, _cellDisplayValues: values }; });
    },
    hydrateCellDisplays: async (cellIds) => {
        const state = get(), channel = state.activeChannel?.id;
        if (!channel)
            return;
        const generation = state._cellDisplayFetch + 1, edits = { ...state._cellDisplayEdits };
        set({ _cellDisplayFetch: generation, cellDisplayLoadError: null, ...(cellIds ? {} : { cellDisplaysReady: false }) });
        const current = () => get().activeChannel?.id === channel && get()._cellDisplayFetch === generation;
        try {
            const records = await fetchCellDisplays(channel);
            if (!current())
                return;
            const results = await Promise.all(Object.entries(records).filter(([id]) => !cellIds || cellIds.includes(id)).map(async ([id, display]) => {
                const loc = get().locateCell(id);
                if (!loc)
                    return null;
                const pinned = loc.cell.pinned && loc.cell.files.some(f => f.name === loc.cell.pinned) ? loc.cell.pinned : null;
                const context = { filePaths: loc.cell.files.map(f => f.name), pinnedPath: pinned };
                const validHistory = new Set<string>();
                let history = display.history;
                if (history) {
                    const inventory = await fetchCellHistory(channel, id);
                    const node = inventory.nodes.find(n => n.node_id === (history?.nodeId ?? inventory.current.node_id));
                    const version = history.versionId ?? node?.versions.at(-1)?.cell_id;
                    if (!node || (history.versionId && !node.versions.some(v => v.cell_id === history?.versionId)))
                        history = null;
                    else {
                        for (const file of node.config)
                            validHistory.add(`config:${file.path}`);
                        for (const file of node.logs)
                            validHistory.add(`log:${file.path}`);
                        if (version)
                            for (const file of (await fetchCellFilesTree(channel, version)).files)
                                validHistory.add(`file:${file.name}`);
                    }
                }
                const validCompare = new Set<string>();
                const tabs = (node: CellDisplay['root']): CellTab[] => node.type === 'leaf' ? node.tabs : node.children.flatMap(tabs);
                const compares = tabs(display.root).filter((t): t is Extract<CellTab, {
                    kind: 'compare';
                }> => t.kind === 'compare');
                const inventories = new Map<string, Promise<{
                    files: {
                        path: string;
                        deleted: boolean;
                    }[];
                }>>();
                await Promise.all(compares.map(async (tab) => {
                    for (const side of [tab.base, tab.current]) {
                        const key = JSON.stringify([side.channel, side.cellId]);
                        if (!inventories.has(key))
                            inventories.set(key, requestJson(`/channels/${encodeURIComponent(side.channel)}/cells/${encodeURIComponent(side.cellId)}/files`, 'GET'));
                        try {
                            const inventory = await inventories.get(key)!;
                            if (!inventory.files.some(f => f.path === side.fileName && !f.deleted))
                                return;
                        }
                        catch (error) {
                            if ((error as {
                                response?: {
                                    status?: number;
                                };
                            }).response?.status === 404)
                                return;
                            throw error;
                        }
                    }
                    validCompare.add(tab.id);
                }));
                return { id, ...reconcileCellDisplay({ ...display, history }, context, t => t.kind === 'history' ? validHistory.has(`${t.category}:${t.path}`) : t.kind === 'compare' ? validCompare.has(t.id) : true) };
            }));
            if (!current())
                return;
            set(prev => {
                const layouts = { ...prev.cellLayouts }, history = { ...prev.cellHistory }, values = { ...prev._cellDisplayValues };
                for (const result of results) {
                    if (!result || (prev._cellDisplayEdits[result.id] ?? 0) !== (edits[result.id] ?? 0))
                        continue;
                    layouts[result.id] = result.layout;
                    values[result.id] = JSON.stringify(encodeCellDisplay(result.layout, result.history));
                    if (result.history)
                        history[result.id] = result.history;
                    else
                        delete history[result.id];
                }
                return { cellLayouts: layouts, cellHistory: history, _cellDisplayValues: values, cellDisplaysReady: true, cellDisplayLoadError: null };
            });
        }
        catch (error) {
            if (current())
                set({ cellDisplaysReady: true, cellDisplayLoadError: error instanceof Error ? error.message : String(error) });
        }
    },
    setTabDrag: drag => set({ tabDrag: drag }),
    setDockDrag: drag => set({ dockDrag: drag }),
});
