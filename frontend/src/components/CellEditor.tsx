import {retainedFileMetadata} from '@/lib/fileDocuments';
import {currentChannelId} from '@/lib/currentChannel';
import { useCellSaveState } from './cellEditor/useCellSaveState';
import {FileOperationNotice} from './FileOperationNotice';
import { useCellFileWrites } from './cellEditor/useCellFileWrites';
import { useCellHistoryPane } from './cellEditor/useCellHistoryPane';
import React, { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useReducedMotion } from 'motion/react';
import type { Cell } from '@/lib/types';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { useFocusActions } from '@/contexts/FocusContext';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useIsZoomTransitioning } from '@/hooks/useZoomTransition';
import { lockOwner } from '@/lib/runningCell';
import type { LockOwnerOf } from '@/lib/fileLocks';
import { isGuestOfFile } from './guestMount';
import type { CellLayout, FilesPaneSide, RunsPaneSide } from '@/stores/collimate/types';
import {
    activeTab as focusedLayoutTab,
    closeTab,
    createDefaultLayout,
    filesPaneCollapsed,
    filesPaneAxis,
    findFileTab,
    FILES_PANE_HEIGHT,
    FILES_PANE_WIDTH,
    openFilePaths,
    openFileTab,
    reconcile,
    resolvePinnedPath,
    RUNS_PANE_HEIGHT,
    RUNS_PANE_WIDTH,
    runsPaneAxis,
    setFilesPane,
    filesPaneRowClass,
    setTabRenderer,
    type LayoutCellContext,
} from '@/lib/cellLayout';
import { FilesPane } from './cellEditor/FilesPane';
import { ContentPane } from './cellEditor/ContentPane';
import { DockPreview } from './cellEditor/DockPreview';
import { RunsPane } from './cellEditor/RunsPane';
import { commandPaneInsetOf, paneHostsPanel, paneInsetTransition } from '@/lib/commandPane';
import { FULL_WINDOW_ATTR } from '@/lib/fullWindowView';
import { CellDiffBanner } from './cellEditor/CellDiffBanner';
import type { CellDiffView } from '@/hooks/useCellDiff';
import { RunnerPicker } from './runner/RunnerPicker';
import { CellPanelConceal, type CellPanelHost } from './cellPanel/CellPanel';
import { ConfirmDialog } from './ConfirmDialog';
import { FilesPanel } from './cellPanel/FilesPanel';
import { TransferPanel } from '@/components/cellPanel/TransferPanel';

// Which cell currently owns mainFocusMode 'file-editor'. Module-level so the
// R12 effect's clear path can tell "we still own the mode" from "another cell
// claimed it in the same commit" (see the effect for the ordering hazard).
let _focusModeOwnerCellId: string | null = null;

interface CellEditorProps {
    cell: Cell;
    /** The cell sits on the Capabilities stack. No run ever holds such a
     *  cell, so it has no runners panel and no status pane. */
    capabilitiesCell?: boolean;
    onUpdate: (cell: Cell) => void;
    isActive?: boolean;
    readOnly?: boolean;
    /** False while the cell is scrolled offscreen (host display:none's the
     *  subtree). Forwarded to every tab's renderer so GPU-backed renderers
     *  repaint when shown again. Defaults to true. */
    visible?: boolean;
    /** Set while this cell is in a cell-pair diff (useCellDiff): the
     *  banner under the header names the base cell and exits the diff;
     *  once the base files have loaded, the file listing shows A/M/D badges
     *  and open file tabs diff against the base's same-named file. The
     *  caller also passes `readOnly` — a cell in diff is never edited. */
    cellDiff?: CellDiffView | null;
}


/** The element a cell's panel renders into: `display: contents`, so the
 *  panel lays out as a child of whichever slot holds it. */
function createPanelBox(): HTMLDivElement {
    const box = document.createElement('div');
    box.className = 'contents';
    return box;
}

/** Put the panel's element in `parent` (the cell's slot or the Commands
 *  pane's), or take it out of the document with null. */
function placePanelBox(box: HTMLDivElement, parent: HTMLElement | null): void {
    if (!parent) box.remove();
    else if (box.parentNode !== parent) parent.append(box);
}
export default function CellEditor({
    cell,
    capabilitiesCell = false,
    onUpdate,
    isActive = false,
    readOnly = false,
    visible = true,
    cellDiff,
}: CellEditorProps) {
    // Reads hit the cell prop (StackItem builds it as
    // ``{...cell, ...cellCache[cell.id]}``); local edits go through
    // ``patchCell`` for the optimistic update + ``onUpdate`` for the
    // server PUT.
    // History mode (the files-pane run-node rail) lives in the store (keyed
    // by cell id) so the selection survives editor open/close and CellEditor
    // remounts (zoom-out / offscreen culling). The content pane stays live
    // while it is on.
    const cellHistory = useCollimateStore(s => s.cellHistory[cell.id]);
    const isHistoryMode = !!cellHistory;
    const isZoomTransitioning = useIsZoomTransitioning();

    // The backend Cell model has a `channel` field but no server-side
    // code actually populates it on cells coming back via state.json —
    // it's always undefined in practice. Fall back to the active
    // channel's UUID so file mutations, checkpoints and history reads have
    // a resolved channel address.
    const activeChannelId = useCollimateStore(s => s.activeChannel?.id);
    const channelId = cell.channel ?? activeChannelId;

    const [versionRefreshNonce, setVersionRefreshNonce] = useState(0);

    // The cell's panel (components/cellPanel): one per cell, of the kind its
    // pick session names. The session is keyed per cell in the store
    // (persisted to the channel's ui_state KV), so it survives this
    // editor's unmounts. A writable cell only; the runners panel never
    // opens on a Capabilities cell, where no run starts. Live runs on the
    // cell never hide it (several runs may share a cell — the runners panel
    // warns and offers a new cell).
    const pickSession = useCollimateStore(s => s.pickSessions[String(cell.id)]);
    // A built-in card (the head of the Capabilities stack): its files are
    // the app's, read-only in the Files pane and every editor, yet unlike a
    // read-only cell it keeps Find file (Open and Info only). It never
    // imports or exports.
    const builtin = !!cell.builtin;
    const locked = readOnly || builtin;
    const panelAllowed = !!pickSession && !readOnly
        && !(capabilitiesCell && pickSession.kind === 'runners')
        && !(builtin && pickSession.kind === 'transfer');
    // The live run owning a path of this cell (its `_runs/<label>/`
    // folder is the lock). Read at call time, so the callback is stable.
    const lockOwnerOf = useCallback<LockOwnerOf>(
        (path) => lockOwner(useCollimateStore.getState(), String(cell.id), path),
        [cell.id],
    );
    const { setMainFocusMode } = useFocusActions();
    // ── Layout (Files pane + content-pane tab/split tree) ───────────────────
    // The tree lives in the store, keyed by cell id, so it survives the
    // unmounts triggered by zoom-out, fan-out toggles and offscreen culling. Every
    // structural mutation goes through a pure op in lib/cellLayout.ts.
    const layout = useCollimateStore(s => s.cellLayouts[cell.id]);
    const seedCellLayout = useCollimateStore(s => s.seedCellLayout);
    const displayReady=useCollimateStore(s=>s.cellDisplaysReady);
    const displayError=useCollimateStore(s=>s.cellDisplayLoadError??s.cellDisplayErrors[cell.id]);
    const updateCellLayout = useCollimateStore(s => s.updateCellLayout);

    // Pinned-ness is DERIVED, never stored: a tab is pinned iff it is the
    // pinned file's tab. A dangling cell.pinned pins
    // nothing.
    const pinnedPath = resolvePinnedPath(cell.pinned, cell.files);
    // Path SETS ride through effects as NUL-joined keys — a primitive dep that
    // ignores array identity and that an effect can split back into a Set. NUL
    // rather than a space: file paths may contain spaces, which would let two
    // different sets collide (and mis-split on the way back out).
    const retained=retainedFileMetadata(channelId||currentChannelId(),cell.id);
    const visibleFilePaths=[...new Set([...cell.files,...retained].map(file=>file.name))];
    const filesFingerprint=visibleFilePaths.join('\u0000');

    // History mode wins the render (the session is preserved in the store
    // and the panel comes back on exit).
    const panel = panelAllowed && !isHistoryMode ? pickSession : null;
    // While the Commands pane is open, the selected cell's picker (Files,
    // runners) renders in the pane's panel slot instead (paneHostsPanel):
    // the cell shows its normal state, unconcealed and undimmed. Closing
    // the pane or selecting another cell clears it and the panel comes
    // back here, its session untouched.
    const paneSlot = useCollimateStore(s => (paneHostsPanel(s, String(cell.id)) ? s.commandPanePanelSlot : null));
    const hostSlot = panel ? paneSlot : null;
    const panelHost: CellPanelHost = hostSlot ? 'pane' : 'cell';
    // The panel always renders into one element of its own, which moves
    // between the cell's slot and the pane's: React keeps the panel mounted
    // across the move, so its unsaved state (a mode, a step, a runner
    // editor's draft) survives the pane opening, closing or the selection
    // passing through another cell.
    const [panelBox] = useState(createPanelBox);
    const cellPanelSlotRef = useRef<HTMLDivElement>(null);
    useLayoutEffect(() => {
        placePanelBox(panelBox, panel ? hostSlot ?? cellPanelSlotRef.current : null);
    });
    useEffect(() => () => placePanelBox(panelBox, null), [panelBox]);

    const layoutCtx = useMemo<LayoutCellContext>(() => ({
        filePaths: new Set(visibleFilePaths),
        pinnedPath,
        // filesFingerprint stands in for the file ARRAY (only names matter to
        // the layout engine, and rebuilding this on every content keystroke
        // would re-run reconcile for nothing).
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }), [filesFingerprint, pinnedPath]);

    // Render-time fallback so the very first paint (before the seeding effect
    // below runs) isn't blank. Only read while the store has no layout yet.
    const fallbackLayout = useMemo(() => createDefaultLayout(layoutCtx), [layoutCtx]);
    const activeLayout: CellLayout = layout ?? fallbackLayout;
    const filesPane = activeLayout.filesPane;
    const filesHidden = filesPaneCollapsed(activeLayout);

    // Seed once per cell. Declared BEFORE the reconcile effect on purpose:
    // effects fire in declaration order, and updateCellLayout no-ops on a cell
    // with no stored layout unless a seed is supplied.
    useEffect(() => {
        if (!useCollimateStore.getState().cellLayouts[cell.id]) {
            seedCellLayout(cell.id, fallbackLayout);
        }
    }, [cell.id, fallbackLayout, seedCellLayout, displayReady]);

    // Bring the stored layout in line with the cell: drop tabs for files that
    // vanished (an active one falls back to a tab no other run owns), ensure
    // the pinned file has a tab. `reconcile` is reference-stable when nothing
    // needs fixing, so the store write bails and this cannot loop. A user pin
    // CHANGE (tracked via the ref — the initial mount never counts) drives
    // BOTH halves of the swap: the newly pinned tab is opened and focused,
    // and the tab the pin moved off is closed. A manual unpin to nothing is
    // the same gesture with no new pin: the tab closes, nothing steals focus.
    const prevPinnedRef = useRef<string | null | undefined>(undefined);
    useEffect(() => {
        const prev = prevPinnedRef.current;
        prevPinnedRef.current = pinnedPath;
        // `prev === undefined` is the initial mount: whatever is pinned then
        // was pinned before this editor existed, so nothing changed and
        // neither half fires.
        const pinChanged = prev !== undefined && prev !== pinnedPath;
        updateCellLayout(cell.id, l => reconcile(l, pinChanged
            ? {
                ...layoutCtx,
                focusPinned: pinnedPath !== null,
                unpinnedPath: prev ?? undefined,
            }
            : layoutCtx));
    }, [cell.id, layoutCtx, pinnedPath, updateCellLayout]);

    // A `.webview` file that vanished (a run's tab file removed at its end)
    // takes its keepAlive native guest with it: nothing else would reclaim
    // the page's OS process.
    const { destroyGuestsWhere, invalidate: invalidateGuestPool } = useGuestPool();
    const prevWebviewsRef = useRef<ReadonlySet<string>>(new Set());
    useEffect(() => {
        const now = new Set(filesFingerprint.split('\u0000').filter(n => n.endsWith('.webview')));
        for (const path of prevWebviewsRef.current) {
            if (!now.has(path)) destroyGuestsWhere(id => isGuestOfFile(id, String(cell.id), path));
        }
        prevWebviewsRef.current = now;
    }, [filesFingerprint, cell.id, destroyGuestsWhere]);

    /** Every layout write in the cell funnels through here. The seed guards
     *  the window between first render and the seeding effect (FilesPane's
     *  child effects run BEFORE ours, and FolderView publishes its folder on
     *  mount). The seed must be the SAME object the first paint rendered —
     *  minting a fresh default here would store different tab ids and remount
     *  every tab renderer one commit after they first mounted. */
    const onLayoutChange = useCallback((fn: (l: CellLayout) => CellLayout) => {
        updateCellLayout(cell.id, fn, () => fallbackLayout);
    }, [cell.id, updateCellLayout, fallbackLayout]);

    const openPaths = useMemo(() => openFilePaths(activeLayout), [activeLayout]);
    const openPathsKey = openPaths.join('\u0000');
    // The focused tab: the active tab of the active leaf.
    const focusedTab = focusedLayoutTab(activeLayout);
    const activeFilePath = focusedTab?.kind === 'file' ? focusedTab.path : null;
    // A focused compare tab is an editor too (its Esc closes it), so it
    // claims mainFocusMode like a file tab (R12 below).
    const activeCompareTabId = focusedTab?.kind === 'compare' ? focusedTab.id : null;

// File operations use pinned bodies and conditional durable receipts.
    const diffReady = !!cellDiff && cellDiff.baseFiles !== null && cellDiff.currentFiles !== null;
    const diffBaseFiles = diffReady ? cellDiff.baseFiles! : undefined;
    const listingCell = useMemo(
        () => (diffReady ? { ...cell, files: cellDiff.currentFiles! } : cell),
        [diffReady, cellDiff, cell],
    );
    const diffLabels = useMemo(
        () => (cellDiff ? { base: cellDiff.baseName || 'base cell', current: cell.name || 'this cell' } : undefined),
        [cellDiff, cell.name],
    );

    const saveState = useCellSaveState({
        cell, channelId, readOnly: locked, onUpdate, openPathsKey,
        onCheckpoint: () => setVersionRefreshNonce(n => n + 1),
    });
    const {
        dirtyByName, lastSavedByName,
        registerEditorHandle, saveNow, prepareViewChange, onBeforeStructuralChange,
    } = saveState;

    // ── mainFocusMode (R12) ────────────────────────────────────────────────
    // Exactly one writer, and only from the ACTIVE cell: every cell the camera
    // has ever passed keeps a mounted editor (StackItem culls with
    // display:none, not unmount), so an unconditional write would let an
    // offscreen cell clobber the focused one's mode. Ownership is a
    // module-level token (not a per-instance ref): when focus hops between two
    // cells in one commit, React runs their effects in TREE order, not
    // gain-before-loss order — the loser may run last, and must not clear a
    // mode the gaining cell just claimed.
    useEffect(() => {
        if (isActive && (activeFilePath !== null || activeCompareTabId !== null)) {
            _focusModeOwnerCellId = cell.id;
            setMainFocusMode('file-editor');
        } else if (_focusModeOwnerCellId === cell.id) {
            _focusModeOwnerCellId = null;
            setMainFocusMode('window');
        }
    }, [cell.id, isActive, activeFilePath, activeCompareTabId, setMainFocusMode]);
    // Release the mode if this editor owned it when it unmounts (cell
    // removed, channel switch). Separate from the effect above: a cleanup
    // there would clear and reclaim on every activeFilePath change.
    useEffect(() => () => {
        if (_focusModeOwnerCellId === cell.id) {
            _focusModeOwnerCellId = null;
            setMainFocusMode('window');
        }
    }, [cell.id, setMainFocusMode]);

    // ── Maximize: the content region, in a body-level portal ────────────────
    // The overlay escapes the canvas pan/zoom transform (a plain
    // `position: fixed` would anchor to the transformed ancestor, not the
    // viewport). Local state is fine: the overlay captures all interaction, so
    // the camera can't move this cell offscreen while it's open.
    // The whole content region (Files pane + tab/split view — everything
    // under the header and the diff banner) expands to the fullscreen
    // overlay as one unit; the tab strip's expand button turns into its
    // inverse (collapse) to bring it back.
    const [contentMaximized, setContentMaximized] = useState(false);
    // Mirror the overlay's existence into the store so the global Esc
    // ladder (capture phase) can defer to the overlay's own handlers.
    const setMaximizedOverlayCellId = useCollimateStore(s => s.setMaximizedOverlayCellId);
    useEffect(() => {
        if (contentMaximized) setMaximizedOverlayCellId(cell.id);
        else if (useCollimateStore.getState().maximizedOverlayCellId === cell.id) {
            setMaximizedOverlayCellId(null);
        }
        return () => {
            if (useCollimateStore.getState().maximizedOverlayCellId === cell.id) {
                setMaximizedOverlayCellId(null);
            }
        };
    }, [contentMaximized, cell.id, setMaximizedOverlayCellId]);
    // The view also closes from outside the cell: the Commands pane lets go
    // of it in the store before a command that acts past this cell
    // (lib/fullWindowView). The cell then flushes and brings the region
    // back, as its own toggle does.
    const heldInStore = useCollimateStore(s => s.maximizedOverlayCellId === cell.id);
    useEffect(() => {
        if (!contentMaximized || heldInStore) return;
        // Just maximised: the effect above has already claimed the store.
        if (useCollimateStore.getState().maximizedOverlayCellId === cell.id) return;
        let live = true;
        void prepareViewChange().then(() => { if (live) setContentMaximized(false); }).catch(() => {});
        return () => { live = false; };
    }, [contentMaximized, heldInStore, cell.id, prepareViewChange]);
    // Toggling remounts the content region (inline ⇄ portal), so flush first —
    // the remounted view restores its locally durable document owner.
    const handleToggleMaximized = useCallback(async () => {
        try {await prepareViewChange();}catch{return;}
        setContentMaximized(prev => !prev);
    }, [prepareViewChange]);

    // ── Opening things ─────────────────────────────────────────────────────
    // openFileTab carries the whole open policy (focus an already-open tab,
    // take over a non-pinned active tab, otherwise insert after it).
    const handleOpenFile = useCallback((path: string) => {
        onLayoutChange(l => openFileTab(l, path, pinnedPath));
    }, [onLayoutChange, pinnedPath]);
    // Double-click in the Files pane: always a NEW tab (the active one is
    // left in place) — an already-open file is just focused.
    const handleOpenFileInNewTab = useCallback((path: string) => {
        onLayoutChange(l => openFileTab(l, path, pinnedPath, undefined, { newTab: true }));
    }, [onLayoutChange, pinnedPath]);
// File operations use pinned bodies and conditional durable receipts.
    const handleOpenFileWith = useCallback(async (path: string, rendererId: string | undefined) => {
        try {await prepareViewChange();}catch{return;}
        onLayoutChange(l => {
            const opened = openFileTab(l, path, pinnedPath, rendererId);
            if (rendererId !== undefined) return opened;
            const found = findFileTab(opened, path);
            return found ? setTabRenderer(opened, found.tab.id, undefined) : opened;
        });
    }, [onLayoutChange, pinnedPath, prepareViewChange]);
    // Closing a FILE tab routes here (strip ✕, context menu, EDITOR_CLOSE) so
    // the checkpoint/baseline effect above sees the path leave the open set.
    const handleCloseFileTab = useCallback(async (path: string) => {
        try {await prepareViewChange();}catch{return;}
        onLayoutChange(l => {
            const found = findFileTab(l, path);
            return found ? closeTab(l, found.tab.id, pinnedPath) : l;
        });
    }, [onLayoutChange, pinnedPath, prepareViewChange]);
    const { handleUpdateFiles, handleTogglePin, fileImportContext } = useCellFileWrites({
        cell, channelId, readOnly: locked, save: saveState, lockOwnerOf, onLayoutChange,
    });

    const { historyAvailable, handleToggleHistory, historyPaneProps, historyView } = useCellHistoryPane({
        cell, channelId, readOnly: locked, cellHistory, versionRefreshNonce,
        onLayoutChange, focusedTab, fileImportContext,
    });

    // Id-keyed close for non-file tabs (a history tab's Esc/✕ must never
    // close the live tab of the same path through the path-keyed handler).
    const handleCloseTabById = useCallback(async (tabId: string) => {
        try {await prepareViewChange();}catch{return;}
        onLayoutChange(l => closeTab(l, tabId, pinnedPath));
    }, [onLayoutChange, pinnedPath, prepareViewChange]);

    // Hidden, the Files pane's toggle leads the first tab strip.
    const handleShowFiles = useCallback(() => {
        onLayoutChange(l => setFilesPane(l, { collapsed: false }));
    }, [onLayoutChange]);

    const cellId = String(cell.id);
    // The side a dock drag of the Files pane's icon would dock it on.
    const filesDockSide = useCollimateStore(s => {
        const drag = s.dockDrag;
        return drag?.cellId === cellId && drag.pane === 'files' ? drag.side as FilesPaneSide | null : null;
    });
    // The side a dock drag of the status pane's caret or Hide icon would
    // dock it on.
    const runsDockSide = useCollimateStore(s => {
        const drag = s.dockDrag;
        return drag?.cellId === cellId && drag.pane === 'runs' ? drag.side as RunsPaneSide | null : null;
    });

    // The status pane (RunsPane) takes room from the panes in one step as
    // it opens, closes, docks or resizes: wake the guest pool for each.
    const runsPane = activeLayout.runsPane;
    useEffect(() => { invalidateGuestPool(); }, [runsPane.open, runsPane.side, runsPane.width, runsPane.height, invalidateGuestPool]);

    // The content region: the pane region (the Files pane, on its dock side
    // inside the cell + the tab/split view, then the status pane on its own
    // dock side, with the cell's panel raised over them), reaching the
    // cell's bottom edge.
    // Rendered EITHER inline OR, maximized, inside the fullscreen overlay
    // as one unit (the expand affordance takes the whole region, not a
    // single renderer).
    //
    // The region and the pane row are each their own stacking context, so
    // a layer inside the row (a file editor at z-50) never paints over the
    // panel. The pane region is relative so the panel's scrim and sheet sit
    // OVER the live panes (from under the cell head to the cell's bottom,
    // the prior view visible beneath).
    const panelNode = panel && <>
        {panel.kind === 'runners' && <RunnerPicker cellId={cellId} host={panelHost} />}
        {panel.kind === 'files' && <FilesPanel cell={cell} channel={channelId ?? ''} layout={activeLayout} onLayoutChange={onLayoutChange}
            activePath={activeFilePath} lockOwnerOf={lockOwnerOf} onUpdateFiles={handleUpdateFiles} host={panelHost}
            onOpenFile={handleOpenFile} onOpenFileWith={(path, rendererId) => { void handleOpenFileWith(path, rendererId); }} />}
        {panel.kind === 'transfer' && <TransferPanel cellId={cellId}
            onClose={() => useCollimateStore.getState().closePickSession(cellId, 'transfer')} />}
    </>;

    const contentRegion = (
        <div data-content-region className="relative isolate flex-1 min-h-0 flex flex-col">
            <div data-pane-region className="relative flex-1 min-h-0 flex flex-col">
                {/* The runs dock region: the Files row first, the status
                    pane last, in a row while the pane docks on the right and
                    a column while it docks at the bottom. The DOM order never
                    changes, so a side change never remounts a renderer. It
                    is a status dock drag's hit area (useRunsDockDrag), and
                    its own stacking context, so the pane's resize strip
                    never paints over the cell's panel. */}
                <div
                    data-runs-dock-region
                    className={`isolate flex-1 min-w-0 min-h-0 flex ${runsPane.side === 'right' ? 'flex-row' : 'flex-col'}`}
                >
                    {/* The pane row: the Files pane first, the content second, on
                        either side; the row's direction puts the pane on its
                        side, so a side change never remounts a renderer. The
                        row is a Files dock drag's hit area (useFilesDockDrag). */}
                    <div
                        data-files-dock-region
                        className={`isolate flex-1 min-w-0 min-h-0 flex overflow-hidden ${filesPaneRowClass(filesPane.side)}`}
                    >
                        <FilesPane
                            cell={listingCell}
                            channel={channelId}
                            layout={activeLayout}
                            onLayoutChange={onLayoutChange}
                            onOpenFile={handleOpenFile}
                            onOpenFileInNewTab={handleOpenFileInNewTab}
                            onOpenFileWith={handleOpenFileWith}
                            onUpdateFiles={handleUpdateFiles}
                            onTogglePin={locked ? undefined : handleTogglePin}
                            readOnly={locked}
                            builtin={builtin && !readOnly}
                            lockOwnerOf={lockOwnerOf}
                            diffTargetFiles={diffBaseFiles}
                            activePath={activeFilePath}
                            // Hidden only on read-only cells — entering
                            // history mid-run is allowed. A cell with no
                            // history yet keeps the button VISIBLE but
                            // greyed with a why-tooltip.
                            onToggleHistory={locked ? undefined : handleToggleHistory}
                            historyDisabled={historyAvailable === false && !isHistoryMode}
                            history={historyPaneProps}
                        />
                        {/* While a panel is up in the cell, pooled renderer
                            iframes (which live in the world-space overlay ABOVE
                            this DOM) are concealed — without it the panel paints
                            under the renderer it is supposed to cover. */}
                        <CellPanelConceal open={panelHost === 'cell' && !!panel}>
                        <ContentPane
                            cell={cell}
                            capabilitiesCell={capabilitiesCell}
                            channel={channelId}
                            layout={activeLayout}
                            onLayoutChange={onLayoutChange}
                            isActive={isActive}
                            // R10: the renderer only paints when the cell is
                            // on screen. A panel dims the panes but
                            // keeps them mounted (the prior view stays
                            // visible beneath it, its pooled iframes
                            // concealed, not released).
                            visible={visible || contentMaximized}
                            readOnly={locked}
                            diffBaseFiles={diffBaseFiles}
                            diffLabels={diffLabels}
                            registerEditorHandle={registerEditorHandle}
                            onBeforeStructuralChange={onBeforeStructuralChange}
                            dirtyByName={dirtyByName}
                            lastSavedByName={lastSavedByName}
                            onFlushNow={locked ? undefined : () => { void saveNow().catch(()=>{}); }}
                            onToggleMaximized={handleToggleMaximized}
                            maximized={contentMaximized}
                            onCloseFileTab={handleCloseFileTab}
                            onCloseTabById={handleCloseTabById}
                            historyView={historyView}
                            onTogglePin={locked ? undefined : handleTogglePin}
                            onUpdateFiles={handleUpdateFiles}
                            filesPane={filesPane}
                            isZoomTransitioning={isZoomTransitioning}
                            onShowFiles={filesHidden ? handleShowFiles : undefined}
                        />
                        </CellPanelConceal>
                    </div>
                    {/* A Capabilities cell has no status pane: a persisted
                        open pane simply does not render there. */}
                    {/* The status pane's composer is a pooled renderer too:
                        concealed under the cell's panel like the content. */}
                    {!capabilitiesCell && (
                        <CellPanelConceal open={panelHost === 'cell' && !!panel}>
                            <RunsPane
                                cellId={cellId}
                                layout={activeLayout}
                                onLayoutChange={onLayoutChange}
                                allowRun={!locked && !isHistoryMode}
                                allowAnnotate={!locked}
                                maximized={contentMaximized}
                            />
                        </CellPanelConceal>
                    )}
                </div>

                {filesDockSide && (
                    <DockPreview
                        side={filesDockSide}
                        size={filesPaneAxis(filesDockSide) === 'x'
                            ? { width: filesPane.width ?? FILES_PANE_WIDTH, maxWidth: '66.6667%' }
                            : { height: filesPane.height ?? FILES_PANE_HEIGHT, maxHeight: '66.6667%' }}
                        label={`Dock files on the ${filesDockSide}`}
                    />
                )}
                {runsDockSide && !capabilitiesCell && (
                    <DockPreview
                        side={runsDockSide}
                        size={runsPaneAxis(runsDockSide) === 'x'
                            ? { width: runsPane.width ?? RUNS_PANE_WIDTH, maxWidth: '66.6667%' }
                            : { height: runsPane.height ?? RUNS_PANE_HEIGHT, maxHeight: '66.6667%' }}
                        label={`Dock status on the ${runsDockSide}`}
                    />
                )}

                {/* The cell's panel, by its session's kind: in the cell, a
                    scrim over the panes (the status pane too), to the
                    region's bottom, and the
                    inset sheet (components/cellPanel), freezing the cell's
                    native guests itself; hosted, in the Commands pane's
                    slot. Either way it renders into its own element
                    (panelBox), which the layout effect above places. */}
                {panelNode && !hostSlot && <div ref={cellPanelSlotRef} data-cell-panel-slot className="absolute inset-0" />}
                {panelNode && createPortal(panelNode, panelBox)}
                {/* A question about this cell's files (delete, overwrite,
                    rename) is the cell's modal, over its panes and panel. */}
                <ConfirmDialog cellId={cellId} />
            </div>
        </div>
    );

    return (
        <div className="flex flex-col h-full relative overflow-hidden">
            {displayError&&<div role="alert" className="text-red text-[0.75rem] p-2">Display state could not be saved or loaded: {displayError} <button onClick={()=>{const store=useCollimateStore.getState();void (store.cellDisplayLoadError?store.hydrateCellDisplays():store.retryCellDisplay(cell.id)).catch(()=>undefined);}}>Retry display state</button></div>}
            {!locked&&!cellDiff&&<FileOperationNotice channel={channelId||currentChannelId()} cellId={cell.id}/>}
            {cellDiff && (
                <CellDiffBanner
                    baseName={cellDiff.baseName}
                    loading={!diffReady}
                    onExit={() => useCollimateStore.getState().exitCellDiff(String(cell.id))}
                />
            )}

            {contentMaximized
                ? <FullscreenContentOverlay spareCellId={cell.id}>{contentRegion}</FullscreenContentOverlay>
                : contentRegion}
        </div>
    );
}

// ---------------------------------------------------------------------------
// FullscreenContentOverlay — body-level portal that hands the cell's whole
// content region (Files pane + tab/split view) the entire app window. No ✕ overlay: the tab strip's expand button turns into its
// inverse (collapse) while maximized and brings the region back. Portaled to <body> so it escapes the canvas pan/zoom transform.
// z-[450] sits above all in-app chrome (top bar, canvas, stack controls) but
// below the shutdown overlay (z-[500]). The Commands pane lies over it
// (CommandPane, z-[455]): the region insets by the pane's width on its side,
// sliding with it, and the view's ground with the strip's line runs on
// under the pane.
// ---------------------------------------------------------------------------
function FullscreenContentOverlay({ children, spareCellId }: {
    children: React.ReactNode;
    /** The hosted cell. Its guests stay live — the maximized region may be
     *  showing exactly that cell's web session — while every other cell's
     *  guests freeze beneath it. */
    spareCellId?: string;
}) {
    useGuestOccluder(spareCellId ? { allExcept: [spareCellId] } : 'all');
    const inset = useCollimateStore(s => commandPaneInsetOf(s.commandPane));
    const reduceMotion = useReducedMotion();
    // The region's guests follow its edges through the slide.
    const { invalidate } = useGuestPool();
    useEffect(() => { invalidate(); }, [inset, invalidate]);
    return createPortal(
        // WebkitAppRegion no-drag: the desktop shell's frameless window keeps
        // its title-bar DRAG band active underneath any DOM stacked over it —
        // z-order doesn't carve it out, only an explicit no-drag does. The
        // overlay's tab strip sits exactly in that band, so without this a
        // real mouse click there DRAGS THE WINDOW (and right-click opens the
        // system menu) while the tabs appear dead. Inert in the browser.
        // The pane's "to cell" puts the keyboard here (lib/fullWindowView).
        <div
            {...{ [FULL_WINDOW_ATTR]: '' }}
            className="fixed inset-0 z-[450] bg-sheet flex flex-col outline-none animate-in fade-in duration-150"
            style={{
                WebkitAppRegion: 'no-drag',
                paddingLeft: inset.left,
                paddingRight: inset.right,
                transition: paneInsetTransition(['padding-left', 'padding-right'], reduceMotion),
            } as React.CSSProperties}
        >
            {/* The strip's line, the window's width, under the region: it
                shows only where the region has slid off it (under the pane,
                and its edge while it slides). */}
            <div aria-hidden data-full-window-ground className="absolute inset-0 pointer-events-none">
                <div className="absolute inset-x-0 top-0 h-[1.875rem] border-b border-line" />
            </div>
            {children}
        </div>,
        document.body,
    );
}
