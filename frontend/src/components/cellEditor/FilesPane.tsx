import React, { useEffect, useRef, useState } from 'react';
import {
    ArrowDownUp, Folder, History, ListTree, TextSearch,
} from 'lucide-react';
import type { Cell } from '@/lib/types';
import type { LockOwnerOf } from '@/lib/fileLocks';
import type { CellLayout, FilesPaneSide, FilesPaneState, FileViewMode } from '@/stores/collimate/types';
import {
    FILES_PANE_MIN_HEIGHT, FILES_PANE_MIN_WIDTH,
    cycleFilesPaneSide, filesPaneAxis, filesPaneCollapsed, filesPaneSize, paneReserve, paneSizeStyle, setFilesPane,
} from '@/lib/cellLayout';
import { FILES_SIDE_ICON, filesDockHint } from '@/lib/filesDock';
import { useFilesDockDrag } from '@/hooks/useFilesDockDrag';
import { parentOf, type UpdateFiles } from '@/lib/fileTransfer';
import { FileBrowser } from '@/components/FileBrowser';
import { FolderView } from '@/components/FolderView';
import { HistoryRail } from './HistoryRail';
import { PaneResizeHandle, type PaneEdge } from './PaneResizeHandle';
import { HistorySections, HistorySectionsProps } from './HistorySections';
import type { HistoryNode } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { railBtn, railBtnOff, railToggleOn } from '@/components/ui/formStyles';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { paneHostsPanel } from '@/lib/commandPane';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';

export interface FilesPaneProps {
    cell: Cell;
    channel?: string;
    layout: CellLayout;
    /** Wraps updateCellLayout(cellId, fn) — every write goes through a pure op. */
    onLayoutChange: (fn: (l: CellLayout) => CellLayout) => void;
    /** Open a file as a content tab (CellEditor: openFileTab). */
    onOpenFile: (path: string) => void;
    /** Double-click: open the file as a NEW tab (never taking the active
     *  tab over). Absent = double-click behaves like a click. */
    onOpenFileInNewTab?: (path: string) => void;
    /** "Open with" a specific renderer (undefined = the primary/default). */
    onOpenFileWith?: (path: string, rendererId: string | undefined) => void;
    onUpdateFiles: UpdateFiles;
    onTogglePin?: (fileName: string) => void;
    /** The whole cell is read-only (review / diff views). */
    readOnly: boolean;
    /** A built-in card: its files are read-only, yet Find file opens (its
     *  Files panel offers Open and Info only); Import / export stays
     *  off. */
    builtin?: boolean;
    /** The live run owning a path: its files are shown locked (lib/fileLocks). */
    lockOwnerOf?: LockOwnerOf;
    diffTargetFiles?: Record<string, string>;
    activePath: string | null;
    /** Enters history mode from the rail's History button. Absent = no
     *  button (read-only cells). Entering history mid-run is allowed. */
    onToggleHistory?: () => void;
    /** True when the cell has no history yet (single version, no runs): the
     *  History button stays VISIBLE but greyed out with an explanatory
     *  tooltip — hidden would read as the feature not existing. */
    historyDisabled?: boolean;
    /** Non-null = history mode: the pane renders the run-node rail + the
     *  Config/Files/Logs sections instead of the file surface. */
    history?: {
        nodes: HistoryNode[];
        selNodeId: string | null;
        selVersionId: string | null;
        nested: boolean;
        onSelect: (nodeId: string, versionId: string | null) => void;
        /** Leave history mode (Tree, Folder and Find file call it). */
        onExit: () => void;
        sections: Omit<HistorySectionsProps, 'nested'>;
    } | null;
}

/** A rail button on (an active view, History, Find file): teal, brass in
 *  the selected cell. */
const railOn = (selected: boolean) => (selected ? 'text-brass bg-brass-soft' : 'text-teal bg-teal-soft');
const ICON = 'w-3.5 h-3.5';

const VIEWS: ReadonlyArray<{ mode: FileViewMode; label: string; icon: React.ComponentType<{ className?: string }> }> = [
    { mode: 'tree', label: 'Tree', icon: ListTree },
    { mode: 'folder', label: 'Folder', icon: Folder },
];

/** Where the pane sits per side: the left column runs the cell's height,
 *  the top band its width. */
const ASIDE_CLASS: Record<FilesPaneSide, string> = {
    left: 'h-full border-r',
    top: 'w-full border-b',
};

/** The pane edge facing the content, where its resize handle sits. */
const CONTENT_EDGE: Record<FilesPaneSide, PaneEdge> = { left: 'right', top: 'bottom' };

/** The cell's file listing (row C12, the mock's `.files`), docked per cell
 *  on the left or top (`filesPane.side`): a 194px column on the left edge,
 *  or a 198px band across the top, inside the cell and resizable by its
 *  content-facing edge between its minimum and two thirds of the cell; the
 *  width and the height are the cell's, persisted apart
 *  (`filesPane.width` / `.height`). The rail's Hide icon names the side;
 *  a right-click on it docks the pane on the other side, and dragging it
 *  to the left or top edge of the cell docks it there (useFilesDockDrag). A
 *  30px icon rail (hide, Tree, Folder, History, then Find file at the far
 *  end) over the view; the Import / export link sits at the tree's bottom
 *  edge. Hidden, it renders nothing and its toggle leads the tab bar
 *  (ContentPane). It owns no file state of its own: the view mode, the
 *  shown/hidden choice and the published folder/selection live in
 *  `layout.filesPane`, so they survive hiding and CellEditor remounts
 *  (zoom-out culling, fan-out toggles). The transfer panel's Import reads
 *  the published folder as its target. */
export const FilesPane: React.FC<FilesPaneProps> = ({
    cell,
    channel,
    layout,
    onLayoutChange,
    onOpenFile,
    onOpenFileInNewTab,
    onOpenFileWith,
    onUpdateFiles,
    onTogglePin,
    readOnly,
    builtin = false,
    lockOwnerOf,
    diffTargetFiles,
    activePath,
    onToggleHistory,
    historyDisabled,
    history,
}) => {
    const historyActive = !!history;
    const selected = useCellSelected();
    const pane = layout.filesPane;
    // Effective show/hide: the user's explicit toggle when set, else hidden.
    const hidden = filesPaneCollapsed(layout);
    const asideRef = useRef<HTMLElement>(null);
    const side = pane.side;
    const axis = filesPaneAxis(side);
    // The size mid-drag; null at rest, when the layout's size shows.
    const [dragSize, setDragSize] = useState<number | null>(null);
    const size = dragSize ?? filesPaneSize(layout);
    const minSize = axis === 'y' ? FILES_PANE_MIN_HEIGHT : FILES_PANE_MIN_WIDTH;
    const reserve = paneReserve(layout, 'files', axis);
    const PanelIcon = FILES_SIDE_ICON[side];
    // Every cell gets Tree (the default) and Folder. Generator / renderer
    // cells manage their definitions from the content pane's pinned Manage
    // tab, not from here.
    const viewMode: FileViewMode = pane.viewMode ?? 'tree';

    // Find file opens the cell's Files panel (row P03), a panel of the
    // cell (components/cellPanel/FilesPanel) over the whole pane region.
    // The rail marks it only there: hosted in the Commands pane, the cell
    // shows its normal state.
    const cellId = String(cell.id);
    const dockDrag = useFilesDockDrag(cellId, onLayoutChange);
    const findingFile = useCollimateStore(s => s.pickSessions[cellId]?.kind === 'files' && !paneHostsPanel(s, cellId));
    const transferring = useCollimateStore(s => s.pickSessions[cellId]?.kind === 'transfer');
    const findFileTitle = useShortcutTooltip(SHORTCUT_IDS.GLOBAL_FIND_FILE, 'Find file');
    const findFile = () => {
        // A panel never covers version history: leave it first.
        if (history) history.onExit();
        useCollimateStore.getState().openPickSession(cellId, 'files');
    };
    // The bottom link opens the cell's transfer panel (Import / Export).
    const openTransfer = () => useCollimateStore.getState().openPickSession(cellId, 'transfer');

    // ── Publishing the import target ──────────────────────────────────────
    // The file surfaces hand us fresh inline closures' worth of updates on
    // every selection change; both surfaces stash OUR callbacks in refs (see
    // FolderView's ref indirection) so this never loops back through them.
    // We still compare before writing: `selection` is an object, so a blind
    // setFilesPane would mint a new layout on every keystroke of navigation.
    const publish = (sel: { path: string; isFolder: boolean } | null, folder?: string) => {
        onLayoutChange(l => {
            const cur = l.filesPane.selection;
            const next = sel ? { nodePath: sel.path, isFolder: sel.isFolder } : null;
            const sameSel = cur === null
                ? next === null
                : next !== null && cur.nodePath === next.nodePath && cur.isFolder === next.isFolder;
            const p: Partial<FilesPaneState> = {};
            if (!sameSel) p.selection = next;
            if (folder !== undefined && folder !== l.filesPane.currentFolder) p.currentFolder = folder;
            return Object.keys(p).length > 0 ? setFilesPane(l, p) : l;
        });
    };

    const surfaceProps = {
        files: cell.files,
        diffTargetFiles,
        onOpenFile,
        onOpenFileInNewTab,
        onOpenFileWith,
        onUpdateFiles,
        pinnedName: cell.pinned,
        onTogglePin,
        activePath,
        readOnly,
        lockOwnerOf,
        // Identity for cross-cell transfer (clipboard/drag provenance).
        cellId: cell.id,
        channel,
        // The in-cell drop target's tag: "Add to <cell name>".
        dropLabel: cell.name,
    };

    // Drop published targets whose node vanished (delete, rename, generator
    // rewrite): an import reading them would silently write into a path that
    // no longer exists. currentFolder trims to the cell root; the selection
    // clears outright. Runs while hidden too (this component stays mounted).
    const filesFingerprint = cell.files.map(f => f.name).join('\u0000');
    useEffect(() => {
        onLayoutChange(l => {
            const { currentFolder, selection } = l.filesPane;
            const names = filesFingerprint.split('\u0000');
            const nodeExists = (p: string) => names.some(n => n === p || n.startsWith(p + '/'));
            const patch2: Partial<FilesPaneState> = {};
            if (currentFolder !== '' && !nodeExists(currentFolder)) patch2.currentFolder = '';
            if (selection && !nodeExists(selection.nodePath)) patch2.selection = null;
            return Object.keys(patch2).length > 0 ? setFilesPane(l, patch2) : l;
        });
        // onLayoutChange is stable per cell; only the file set matters here.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filesFingerprint]);

    if (hidden) return null;

    const surface = history ? (
        <HistorySections {...history.sections} nested={history.nested} />
    ) : viewMode === 'folder' ? (
        <FolderView
            {...surfaceProps}
            // Resume where the pane left off — the published folder outlives
            // this component (hiding, offscreen culling), and a remount that
            // reset to the root would silently retarget the import target.
            initialFolder={pane.currentFolder}
            onCurrentFolderChange={(path) => onLayoutChange(l =>
                path === l.filesPane.currentFolder ? l : setFilesPane(l, { currentFolder: path }))}
            onSelectionChange={(sel) => publish(sel)}
        />
    ) : (
        <FileBrowser
            {...surfaceProps}
            // The tree has no "current folder" of its own, so the import target
            // derives from the selection: a folder is itself, a file is its
            // parent directory, nothing selected is the cell root.
            onSelectionChange={(sel) => publish(sel, sel ? (sel.isFolder ? sel.path : parentOf(sel.path)) : '')}
        />
    );

    const on = railOn(selected);
    const hideTitle = `${historyActive ? 'Leave history to hide files' : 'Hide files'} · ${filesDockHint(side)}`;
    const noHistoryYet = !!historyDisabled && !historyActive;

    return (
        <aside
            ref={asideRef}
            aria-label="Files"
            data-files-side={side}
            className={cn(
                'relative flex-none flex flex-col min-h-0 border-line bg-sheet text-[0.78125rem] text-ink-2',
                ASIDE_CLASS[side],
            )}
            // The stored size, held between the minimum and two thirds of the cell
            // as the cell itself narrows or widens, and short of the content's
            // minimum beside it.
            style={paneSizeStyle(axis, size, minSize, reserve.css)}
        >
            {/* The content-facing edge's resize strip, out of the flow. */}
            <PaneResizeHandle
                paneRef={asideRef}
                edge={CONTENT_EDGE[side]}
                size={size}
                min={minSize}
                reserve={reserve.px}
                label="files"
                onPreview={setDragSize}
                onCommit={(v) => onLayoutChange(l => setFilesPane(l, axis === 'y' ? { height: v } : { width: v }))}
                onCollapse={historyActive ? undefined : () => onLayoutChange(l => setFilesPane(l, { collapsed: true }))}
            />
            {/* The rail: the hide toggle first (the same control the tab
                bar leads with while the pane is hidden), the views, then
                Find file at the far end. Its buttons sit on the tab
                strip's line (TabStrip TOOL: 0.25rem above the strip's
                bottom): the 1px border and the rest of that 0.25rem as
                padding, so the toggle stays put as the pane shows and
                hides. */}
            <div className="flex-none h-[1.875rem] flex items-end gap-0.5 pb-[calc(0.25rem-1px)] px-1.5 border-b border-line select-none">
                {/* The dock gestures sit on a wrapper: in history mode the
                    button is disabled (and passes the pointer through), yet
                    a right-click still docks on the next side and a drag
                    still docks on an edge. */}
                <span
                    data-files-dock-handle
                    className="inline-flex mr-0.5"
                    title={hideTitle}
                    onContextMenu={(e) => { e.preventDefault(); onLayoutChange(cycleFilesPaneSide); }}
                    {...dockDrag}
                >
                    <button
                        type="button"
                        aria-label="Hide files"
                        aria-pressed
                        // History mode lives in the pane: hiding it would
                        // strand an invisible mode.
                        disabled={historyActive}
                        title={hideTitle}
                        onClick={() => onLayoutChange(l => setFilesPane(l, { collapsed: true }))}
                        className={cn(railBtn, railBtnOff, railToggleOn, 'disabled:pointer-events-none')}
                    >
                        <PanelIcon className={ICON} />
                    </button>
                </span>
                {VIEWS.map(({ mode, label, icon: Icon }) => {
                    const active = !historyActive && viewMode === mode;
                    return (
                        <button
                            key={mode}
                            type="button"
                            aria-label={label}
                            title={label}
                            aria-pressed={active}
                            onClick={() => {
                                if (history) history.onExit();
                                onLayoutChange(l => setFilesPane(l, { viewMode: mode }));
                            }}
                            className={cn(railBtn, active ? on : railBtnOff)}
                        >
                            <Icon className={ICON} />
                        </button>
                    );
                })}
                {(onToggleHistory || historyActive) && (
                    <button
                        type="button"
                        aria-label="History"
                        aria-pressed={historyActive}
                        disabled={noHistoryYet}
                        title={noHistoryYet
                            ? 'No history yet — versions appear after an edit checkpoint or a run'
                            : 'Version history'}
                        onClick={noHistoryYet || historyActive ? undefined : onToggleHistory}
                        className={cn(railBtn, historyActive ? on : railBtnOff)}
                    >
                        <History className={ICON} />
                    </button>
                )}
                <span className="flex-1" />
                <button
                    type="button"
                    aria-label="Find file"
                    // No panel shows on a read-only cell (a cell diff).
                    disabled={readOnly && !builtin}
                    title={readOnly && !builtin ? 'Read-only: no file actions here' : findFileTitle}
                    aria-expanded={findingFile && !historyActive}
                    onClick={findFile}
                    className={cn(railBtn, findingFile && !historyActive ? on : railBtnOff)}
                >
                    <TextSearch className={ICON} />
                </button>
            </div>

            <div className="flex-1 min-h-0 min-w-0 flex">
                {history && (
                    <HistoryRail
                        nodes={history.nodes}
                        selNodeId={history.selNodeId}
                        selVersionId={history.selVersionId}
                        nested={history.nested}
                        onSelect={history.onSelect}
                    />
                )}
                {/* A flex column, not a plain block: the surfaces size
                    themselves with flex-1/min-h-0 and would never scroll
                    inside a block wrapper. */}
                <div className="flex-1 min-h-0 min-w-0 flex flex-col">{surface}</div>
            </div>

            {/* The Import / export link, at the tree's bottom edge, opening
                the cell's transfer panel. Hidden in history mode (the
                published import target must never point at historical
                paths); disabled on a read-only cell, where no panel shows. */}
            {!historyActive && (
                <div className="flex-none flex flex-col px-1 pb-1.5">
                    <button
                        type="button"
                        onClick={openTransfer}
                        disabled={readOnly}
                        title={builtin ? 'Built-in packages are part of the app'
                            : readOnly ? 'Read-only: no file actions here' : 'Import files into this cell or export it'}
                        aria-expanded={transferring}
                        className="flex items-center gap-1.5 h-6 px-1.5 rounded-xs text-[0.78125rem] text-left text-ink-2 hover:bg-sheet-2 hover:text-ink disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-ink-2"
                    >
                        <ArrowDownUp className={cn(ICON, 'flex-none text-ink-3')} />
                        <span className="truncate">Import / export</span>
                    </button>
                </div>
            )}
        </aside>
    );
};
