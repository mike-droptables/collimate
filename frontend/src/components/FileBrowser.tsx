import {FileConflictNotice} from './FileConflictNotice';
import {currentChannelId} from '@/lib/currentChannel';
import React, { useCallback, useMemo, useRef, useState } from 'react';
import type { VirtualFile } from '@/lib/types';
import {useEventCallback} from '@/hooks/useEventCallback';
import { ScrollArea } from '@/components/ui/scroll-area';
import { isTypingTarget } from '@/lib/keybindings/match';
import { lockOwnerUnder, type LockOwnerOf } from '@/lib/fileLocks';
import { FileTree, CollapseSignal, FileRowTransfer } from '@/components/FileTreeNode';
import { buildFileTree, augmentTreeWithDeleted, TreeNode } from '@/lib/fileTree';
import { parentOf, type UpdateFiles } from '@/lib/fileTransfer';
import { captureDrop, hasDroppedItems, type DropCapture } from '@/lib/dropEntries';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useFileOperations } from '@/hooks/useFileOperations';
import { useFileTransfer, } from '@/hooks/useFileTransfer';
import { useFilesPanePaste } from '@/hooks/useFilesPanePaste';
import { AddFileDialog } from '@/components/AddFileDialog';
import type { FileMenuTarget } from '@/components/FileContextMenu';
import { CellFileContextMenu } from '@/components/CellFileContextMenu';
import { InCellDropFrame } from '@/components/InCellDropFrame';
import { useInCellDropFrame } from '@/hooks/useInCellDropFrame';
import { isRunPath } from '@/lib/runningCell';
import { isEditableTarget } from '@/lib/nativeSelection';

/** The props shared by both file surfaces (the tree FileBrowser and the
 *  grid FolderView); each view's own props extend this. */
export interface FileSurfaceProps {
    files: VirtualFile[];
    diffTargetFiles?: Record<string, string>;
    /** Open a file by PATH. Content tabs are path-keyed; the array index a
     *  tree node carries is an internal detail of rename/delete only (an
     *  index is not repaired across marker-pruning, a path always is). */
    onOpenFile: (path: string) => void;
    /** Double-click: open as a NEW tab (see FileTreeNode). */
    onOpenFileInNewTab?: (path: string) => void;
    /** "Open with" — open the file as a content tab rendered by a specific
     *  renderer (undefined = the primary/default resolution). Absent → the
     *  context-menu row is hidden (read-only embeds like the diff browser). */
    onOpenFileWith?: (path: string, rendererId: string | undefined) => void;
    onUpdateFiles?: UpdateFiles;
    /** Path (within `files`) currently pinned as the cell's preview, if any. */
    pinnedName?: string;
    /** Toggle the pinned file. Omitted/undefined disables the pin affordance. */
    onTogglePin?: (fileName: string) => void;
    /** Path of the focused file tab — exposed as the row's accessible current state. */
    activePath?: string | null;
    readOnly?: boolean;
    /** The live run owning a path: its files are shown locked (lib/fileLocks). */
    lockOwnerOf?: LockOwnerOf;
    // Identity for cross-cell transfer (clipboard/drag provenance). Absent in
    // read-only embeds (the history sections) → transfer features are inert.
    cellId?: string;
    channel?: string;
    /** The cell's name, for the in-cell drop target's tag ("Add to <name>"). */
    dropLabel?: string;
    // ── Upward reporting (the transfer panel's import target) ─────────────
    /** Report the selected node (or null) upward: the Files pane derives the
     *  import target folder from it. */
    onSelectionChange?: (sel: { path: string; isFolder: boolean } | null) => void;
}

export const FileBrowser: React.FC<FileSurfaceProps> = ({
    files,
    diffTargetFiles,
    onOpenFile,
    onOpenFileInNewTab,
    onOpenFileWith,
    onUpdateFiles,
    pinnedName,
    onTogglePin,
    activePath = null,
    readOnly = false,
    lockOwnerOf,
    cellId,
    channel,
    dropLabel,
    onSelectionChange,
}) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const dropFrame = useInCellDropFrame();

    // Build tree from all files (preserving original indices for rename/delete
    // operations). In diff mode, augment with "deleted" virtual nodes.
    const tree = useMemo(() => {
        const t = buildFileTree(files);
        if (diffTargetFiles) {
            const currentNames = new Set(files.map(f => f.name));
            augmentTreeWithDeleted(t, diffTargetFiles, currentNames);
        }
        return t;
    }, [files, diffTargetFiles]);

    const filePaths = useMemo(() => files.map(f => f.name), [files]);

    const { handleCreateFileAtPath, handleCreateFolder, handleDuplicate, handleRename, handleDelete, handleDropFiles } =
        useFileOperations({ files, onUpdateFiles, readOnly, lockOwnerOf, channel, cellId });

    /** One guarded entry point for every OS-file drop this browser owns — the
     *  root zone below and every folder row in the tree. A DOM drop handler
     *  can't await, so the import promise is deliberately floated; the catch is
     *  the last-resort guard that keeps a failed drop from vanishing as an
     *  unhandled rejection (handleDropFiles already isolates per-file failures
     *  and reports the rest itself, so reaching this is a bug, not a bad file). */
    const dropFilesInto = useCallback((capture: DropCapture, targetPath: string) => {
        void handleDropFiles(capture, targetPath)
            .catch(err => console.error('[drop] importing the dropped files failed', err));
    }, [handleDropFiles]);

    const fileTransfer = useFileTransfer({ cellId: cellId ?? '', channel, files, onUpdateFiles, readOnly, lockOwnerOf });
    const transferEnabled = !!cellId;

    /** The focused row in THIS browser (several file surfaces can be mounted
     *  at once, a Files pane per cell), or null. */
    const focusedRow = (): { path: string; kind: 'file' | 'folder' } | null => {
        const active = document.activeElement as HTMLElement | null;
        const row = active && containerRef.current?.contains(active)
            ? (active.closest('[data-filenode-path]') as HTMLElement | null)
            : null;
        const path = row?.getAttribute('data-filenode-path');
        const kind = row?.getAttribute('data-filenode-kind') as 'file' | 'folder' | null | undefined;
        return path && kind ? { path, kind } : null;
    };
    // A paste lands in the focused folder row, beside a focused file row, or
    // at the cell root.
    const paste = useFilesPanePaste({
        containerRef, files, cellId, readOnly, onUpdateFiles, onOpenFile, pasteInto: fileTransfer.pasteInto,
        targetFolder: () => {
            const row = focusedRow();
            return row ? (row.kind === 'folder' ? row.path : parentOf(row.path)) : '';
        },
    });

    // Collapse-all / expand-all broadcast.
    const [collapseSignal, setCollapseSignal] = useState<CollapseSignal>({ nonce: 0, collapsed: false });
    const expandAll = () => setCollapseSignal(s => ({ nonce: s.nonce + 1, collapsed: false }));
    const collapseAll = () => setCollapseSignal(s => ({ nonce: s.nonce + 1, collapsed: true }));

    // Add file/folder dialog + context menu state.
    const [addDialog, setAddDialog] = useState<{ x: number; y: number; basePath: string; mode: 'file' | 'folder' } | null>(null);
    const [menu, setMenu] = useState<{ x: number; y: number; target: FileMenuTarget } | null>(null);
    // Path whose row should enter inline-rename mode (driven by the menu).
    const [renamePath, setRenamePath] = useState<string | null>(null);

    const openAdd = (mode: 'file' | 'folder', basePath: string, anchor?: { left: number; bottom: number }) => {
        setAddDialog({
            mode,
            basePath,
            x: anchor ? anchor.left : 120,
            y: anchor ? anchor.bottom + 4 : 80,
        });
    };

    const submitAdd = async (name: string) => {
        if (!addDialog) return;
        // A refused create keeps the dialog open: it shows the returned error.
        if (addDialog.mode === 'folder') {
            const res = await handleCreateFolder(addDialog.basePath, name);
            if (res.ok) setAddDialog(null);
            return res;
        }
        const res = await handleCreateFileAtPath(name, addDialog.basePath);
        if (res.ok) {
            onOpenFile(res.path);
            setAddDialog(null);
        }
        return res;
    };

    const onDropRoot = (e: React.DragEvent) => {
        // Internal cell-to-cell transfer drops at the cell root.
        if (fileTransfer.dropInto('', e)) return;
        e.preventDefault();
        e.stopPropagation();
        if (readOnly) return;
        // Snapshot the drop BEFORE anything async: a dropped FOLDER is only
        // reachable through dataTransfer.items, which the browser clears the
        // instant this handler returns (see lib/dropEntries).
        const capture = captureDrop(e.dataTransfer);
        if (hasDroppedItems(capture)) dropFilesInto(capture, '');
    };

    const onDragOver = (e: React.DragEvent) => {
        if (fileTransfer.isInternalDrag(e)) {
            // Stop here so the canvas's own dragover (cross-cell rule) can't
            // rewrite the effect this cell's drop will actually perform.
            e.stopPropagation();
            if (readOnly) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = fileTransfer.dropEffectFor(e);
            return;
        }
        // A read-only pane takes no drop. Own the drag without accepting
        // it: the cursor shows no-drop, no drop fires, the browser default
        // (an OS file would open in the tab) never runs, and the canvas's
        // NEW CELL slot stays down.
        if (readOnly) {
            e.preventDefault();
            e.stopPropagation();
            e.dataTransfer.dropEffect = 'none';
            return;
        }
        e.preventDefault();
        e.stopPropagation();
    };

    // An OS file drag over this writable tree lights the in-cell drop frame,
    // over the root and every row: a file row hands the drag to the root, a
    // folder row takes it into its folder (and rings itself). A `_runs`
    // folder row refuses it, so the frame goes dark there. Capture phase: a
    // folder row stops its own dragover.
    const aimDropFrame = (e: React.DragEvent) => {
        const row = e.target instanceof Element ? e.target.closest('[data-filenode-kind="folder"]') : null;
        const folder = row?.getAttribute('data-filenode-path');
        dropFrame.aim(e, folder && isRunPath(folder) ? null : '');
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if (!transferEnabled || isTypingTarget(e.nativeEvent)) return;
        const mod = e.ctrlKey || e.metaKey;
        if (!mod) return;
        const k = e.key.toLowerCase();
        // Mod+V is left to the browser: its `paste` event reaches
        // useFilesPanePaste with the OS clipboard's contents.
        if (k !== 'c' && k !== 'x') return;
        const row = focusedRow();
        if (!row) return;
        e.preventDefault();
        const node = { name: row.path.split('/').pop() ?? row.path, path: row.path, type: row.kind, children: {} } as TreeNode;
        if (k === 'c') fileTransfer.copyNode(node);
        else fileTransfer.cutNode(node);
    };

    // Row-level transfer + context-menu wiring threaded to every tree row.
    const openFile = useEventCallback(onOpenFile);
    const openNewTab = useEventCallback((path: string) => onOpenFileInNewTab?.(path));
    const reportSelection = useEventCallback((selection: {path: string; isFolder: boolean} | null) => onSelectionChange?.(selection));
    const createFile = useEventCallback((basePath: string, anchor?: {left: number; bottom: number}) => openAdd('file', basePath || '', anchor));
    const rename = useEventCallback(handleRename);
    const remove = useEventCallback(handleDelete);
    const pin = useEventCallback((path: string) => onTogglePin?.(path));
    const dropFiles = useEventCallback(dropFilesInto);
    const contextMenu = useEventCallback((node: TreeNode, x: number, y: number) => setMenu({x,y,target:{kind:node.type,node} as FileMenuTarget}));
    const dragStart = useEventCallback(fileTransfer.startDrag);
    const dragEnd = useEventCallback(fileTransfer.endDrag);
    const internalDrag = useEventCallback(fileTransfer.isInternalDrag);
    const dropEffect = useEventCallback(fileTransfer.dropEffectFor);
    const dropInto = useEventCallback(fileTransfer.dropInto);
    const renameConsumed = useEventCallback(() => setRenamePath(null));
    const rowTransfer: FileRowTransfer | undefined = useMemo(() => transferEnabled ? {
        onContextMenu: contextMenu, onDragStartNode: dragStart, onDragEndNode: dragEnd,
        isInternalDrag: internalDrag, dropEffectFor: dropEffect, onDropInto: dropInto,
        renamePath, onRenameConsumed: renameConsumed,
    } : undefined, [transferEnabled, contextMenu, dragStart, dragEnd, internalDrag, dropEffect, dropInto, renamePath, renameConsumed]);
    const liveRuns = useCollimateStore(state => state.runs);
    const hasNewTab = !!onOpenFileInNewTab;
    const hasSelection = !!onSelectionChange;
    const hasPin = !!onTogglePin;
    const treeElement = useMemo(() => {
        void liveRuns; // A live-lock change requires a fresh tree element.
        return <FileTree
        nodes={tree} onOpenFile={openFile} onOpenFileInNewTab={hasNewTab ? openNewTab : undefined}
        activePath={activePath} onSelectionChange={hasSelection ? reportSelection : undefined} level={0}
        onCreateFile={createFile} onRename={rename} onDelete={remove}
        pinnedName={pinnedName} onTogglePin={hasPin ? pin : undefined} readOnly={readOnly}
        lockOwnerOf={lockOwnerOf} onDropFiles={dropFiles} transfer={rowTransfer}
        diffTargetFiles={diffTargetFiles} collapseSignal={collapseSignal}
    />;
    }, [tree,openFile,hasNewTab,openNewTab,activePath,hasSelection,reportSelection,createFile,rename,remove,
        pinnedName,hasPin,pin,readOnly,lockOwnerOf,dropFiles,rowTransfer,diffTargetFiles,collapseSignal,liveRuns]);

    return (
        <div
            ref={containerRef}
            className="relative flex flex-col h-full bg-transparent outline-none"
            tabIndex={-1}
            onKeyDown={onKeyDown}
            onDrop={onDropRoot}
            onDragOver={onDragOver}
            onDragOverCapture={readOnly ? undefined : aimDropFrame}
            {...dropFrame.frameProps}
            onContextMenu={(e) => {
                // Right-click on the tree's background: the root's menu, its
                // New file / New folder / Paste and the tree's Expand all /
                // Collapse all (the files pane has no toolbar: its rail sits
                // straight over the tree, as the mock's `.files`). A rename
                // field keeps the browser's own menu (isEditableTarget).
                if (isEditableTarget(e.target)) return;
                e.preventDefault();
                setMenu({ x: e.clientX, y: e.clientY, target: { kind: 'empty', basePath: '' } });
            }}
        >
            {/* Rows are exactly the list's width (fitWidth), so a long name
                truncates instead of pushing the row — and its hover actions —
                past the pane. The list is the rows' @container; its padding
                is the mock's `.tree` (6px 4px). */}
            {/* type="scroll": the scrollbar shows only while scrolling, so it
                never pops in and out as the pointer crosses the list. */}
            {cellId&&!readOnly&&<FileConflictNotice channel={channel||currentChannelId()} cellId={cellId} onOpenFile={onOpenFile}/>}
            <ScrollArea fitWidth type="scroll" className="flex-1">
                <div className="@container py-1.5 px-1">
                    {Object.keys(tree).length === 0 && (
                        <div className="text-center py-4 text-xs text-ink-3 italic border border-dashed border-line-2 rounded-md m-2">
                            Drop files here, or right-click for a new file
                        </div>
                    )}
                    {treeElement}
                </div>
            </ScrollArea>

            {dropFrame.target !== null && <InCellDropFrame cellName={dropLabel} />}

            {addDialog && (
                <AddFileDialog
                    x={addDialog.x}
                    y={addDialog.y}
                    basePath={addDialog.basePath}
                    existingNames={files.map(f => f.name)}
                    mode={addDialog.mode}
                    onCancel={() => setAddDialog(null)}
                    onCreate={submitAdd}
                />
            )}
            {paste.prompt}

            {menu && (
                <CellFileContextMenu menu={menu} readOnly={readOnly} locked={menu.target.kind !== 'empty' && (isRunPath(menu.target.node.path)||!!lockOwnerUnder(filePaths, menu.target.node.path, menu.target.kind === 'folder', lockOwnerOf))} transfer={fileTransfer} pasteHere={transferEnabled ? paste.pasteHere : undefined} cellId={cellId} pinnedName={pinnedName} onClose={()=>setMenu(null)} onOpen={menu.target.kind === 'file' && menu.target.node.fileData ? () => onOpenFile(menu.target.kind === 'file' ? menu.target.node.fileData!.name : '') : undefined} onRename={menu.target.kind !== 'empty' ? () => setRenamePath((menu.target as {node:TreeNode}).node.path) : undefined} onOpenFileWith={onOpenFileWith} onDelete={handleDelete} onDuplicate={handleDuplicate} onTogglePin={onTogglePin} onNewFile={base=>openAdd('file',base,{left:menu.x,bottom:menu.y})} onNewFolder={base=>openAdd('folder',base,{left:menu.x,bottom:menu.y})} onExpandAll={menu.target.kind === 'empty' ? expandAll : undefined} onCollapseAll={menu.target.kind === 'empty' ? collapseAll : undefined} />
            )}
        </div>
    );
};
