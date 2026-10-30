import type { CellHistoryState, CellLayout, CellTab, ContentNode, FilesPaneSide, RunsPaneState } from '@/stores/collimate/types';
import { reconcile, type LayoutCellContext } from './cellLayout';
export type DisplayNode = {
    type: 'leaf';
    id: string;
    tabs: CellTab[];
    activeTabId: string | null;
} | {
    type: 'split';
    id: string;
    dir: 'row' | 'column';
    children: DisplayNode[];
};
export interface CellDisplay {
    version: 1;
    root: DisplayNode;
    activeLeafId: string;
    filesPane: {
        collapsed: boolean | null;
        side: FilesPaneSide;
        /** px, the left column's width; null = the default. */
        width: number | null;
        /** px, the top band's height; null = the default. */
        height: number | null;
        viewMode: 'tree' | 'folder';
        panelFilters: {
            runsOnly: boolean;
            sortByRecency: boolean;
        };
    };
    /** The runs pane: open, its side, the right column's width and the
     *  bottom band's height (null = the defaults). Exactly these four. */
    runsPane: RunsPaneState;
    pinnedTabCompact: boolean;
    history: CellHistoryState | null;
}
const side = (value: Extract<CellTab, {
    kind: 'compare';
}>['base']) => ({ channel: value.channel, cellId: value.cellId, fileName: value.fileName, label: value.label });
function tab(value: CellTab): CellTab {
    const head = { id: value.id, kind: value.kind };
    switch (value.kind) {
        case 'file': return { ...head, kind: 'file', path: value.path, ...(value.rendererId === undefined ? {} : { rendererId: value.rendererId }) };
        case 'history': return { ...head, kind: 'history', category: value.category, path: value.path, ...(value.rendererId === undefined ? {} : { rendererId: value.rendererId }) };
        case 'compare': return { ...head, kind: 'compare', base: side(value.base), current: side(value.current) };
        case 'empty': return { ...head, kind: 'empty' };
    }
}
const runsPane = (value: RunsPaneState): RunsPaneState =>
    ({ open: value.open, side: value.side, width: value.width, height: value.height });
export function encodeCellDisplay(layout: CellLayout, history: CellHistoryState | undefined | null): CellDisplay {
    const node = (value: ContentNode): DisplayNode => value.type === 'leaf'
        ? { type: 'leaf', id: value.id, tabs: value.tabs.map(tab), activeTabId: value.activeTabId }
        : { type: 'split', id: value.id, dir: value.dir, children: value.children.map(node) };
    return { version: 1, root: node(layout.root), activeLeafId: layout.activeLeafId,
        filesPane: { collapsed: layout.filesPane.collapsed, side: layout.filesPane.side, width: layout.filesPane.width, height: layout.filesPane.height, viewMode: layout.filesPane.viewMode ?? 'tree', panelFilters: { ...layout.filesPane.panelFilters } },
        runsPane: runsPane(layout.runsPane),
        pinnedTabCompact: layout.pinnedTabCompact ?? false,
        history: history ? { nodeId: history.nodeId, versionId: history.versionId, sectionsOpen: { ...history.sectionsOpen } } : null };
}
export function decodeCellDisplay(display: CellDisplay): {
    layout: CellLayout;
    history: CellHistoryState | null;
} {
    if (display.version !== 1)
        throw new Error('Unsupported cell display version');
    const node = (value: DisplayNode): ContentNode => value.type === 'leaf'
        ? { ...value, tabs: value.tabs.map(tab) }
        : { type: 'split', id: value.id, dir: value.dir, children: value.children.map(node), sizes: value.children.map(() => 1 / value.children.length) };
    return { layout: { root: node(display.root), activeLeafId: display.activeLeafId,
            filesPane: { ...display.filesPane, panelFilters: { ...display.filesPane.panelFilters }, currentFolder: '', selection: null },
            runsPane: runsPane(display.runsPane), pinnedTabCompact: display.pinnedTabCompact },
        history: display.history ? { ...display.history, sectionsOpen: { ...display.history.sectionsOpen } } : null };
}
/** Coordinate validation is supplied by current accepted/history inventories. */
export function reconcileCellDisplay(display: CellDisplay, context: LayoutCellContext, valid: (tab: CellTab) => boolean): ReturnType<typeof decodeCellDisplay> {
    const restored = decodeCellDisplay(display);
    const filter = (node: ContentNode): ContentNode => node.type === 'split' ? { ...node, children: node.children.map(filter) }
        : { ...node, tabs: node.tabs.filter(valid) };
    return { ...restored, layout: reconcile({ ...restored.layout, root: filter(restored.layout.root) }, context) };
}
