import {FileConflictNotice} from './FileConflictNotice';
import {currentChannelId} from '@/lib/currentChannel';
import { carriesOsFiles } from '@/lib/fileDrag';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Folder, SquarePlus, FolderPlus, ChevronRight, ArrowUp, Pin, Lock } from 'lucide-react';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Input } from '@/components/ui/input';
import { buildFileTree, augmentTreeWithDeleted, fileDiffStatus, TreeNode } from '@/lib/fileTree';
import { treeNodeOrder } from '@/lib/fileSearch';
import { parentOf } from '@/lib/fileTransfer';
import { captureDrop, hasDroppedItems } from '@/lib/dropEntries';
import { getFileIcon } from '@/components/fileIcon';
import { isRunPath } from '@/lib/runningCell';
import { lockTitle, nodeLockOwner } from '@/lib/fileLocks';
import { useFileOperations } from '@/hooks/useFileOperations';
import { useFileTransfer, } from '@/hooks/useFileTransfer';
import { useFilesPanePaste } from '@/hooks/useFilesPanePaste';
import { AddFileDialog } from '@/components/AddFileDialog';
import type { FileMenuTarget } from '@/components/FileContextMenu';
import { CellFileContextMenu } from '@/components/CellFileContextMenu';
import { InCellDropFrame } from '@/components/InCellDropFrame';
import { useInCellDropFrame } from '@/hooks/useInCellDropFrame';
import type { FileSurfaceProps } from '@/components/FileBrowser';
import { iconBtn } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { isTypingTarget } from '@/lib/keybindings/match';
import { isEditableTarget } from '@/lib/nativeSelection';

/** Descend the tree to a folder path; returns that folder's children, or null
 *  if the path no longer resolves to a folder. */
function childrenAt(root: Record<string, TreeNode>, path: string): Record<string, TreeNode> | null {
    if (!path) return root;
    let level = root;
    for (const part of path.split('/')) {
        const n = level[part];
        if (!n || n.type !== 'folder') return null;
        level = n.children;
    }
    return level;
}

/** Resolve a node by its full path, or null if it no longer exists. */
function findNode(root: Record<string, TreeNode>, path: string): TreeNode | null {
    if (!path) return null;
    const segs = path.split('/');
    let level = root;
    for (let i = 0; i < segs.length; i++) {
        const n = level[segs[i]];
        if (!n) return null;
        if (i === segs.length - 1) return n;
        if (n.type !== 'folder') return null;
        level = n.children;
    }
    return null;
}

export interface FolderViewProps extends FileSurfaceProps {
    /** The folder to start in — the parent's published currentFolder, so a
     *  remount (collapse, offscreen culling) resumes where the pane left off
     *  instead of silently retargeting the import target at the cell root.
     *  Ignored when it no longer names an existing folder. */
    initialFolder?: string;
    /** Report this browser's current folder upward — the transfer panel's
     *  import target. In tree mode the folder is derived from the selection
     *  by the parent. */
    onCurrentFolderChange?: (path: string) => void;
}

/** Traditional "folder with icons" layout for a cell's files. Renders only the
 *  current folder's contents (breadcrumb navigation), double-click to enter a
 *  folder / open a file. Shares the clipboard + drag-and-drop engine with the
 *  tree view. */
/** A files-rail tool (the mock's `.files .rail button`): 18px, ink-3, the hover fill on sheet-2. */
const RAIL_BUTTON = cn(iconBtn, 'h-[1.125rem] px-[0.3125rem]');

export const FolderView: React.FC<FolderViewProps> = ({
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
    initialFolder,
    onCurrentFolderChange,
    onSelectionChange,
}) => {
    // Resume at the parent's published folder; a stale pointer (the folder
    // vanished while this view was unmounted) falls back to the root.
    const [currentPath, setCurrentPath] = useState(() =>
        initialFolder && files.some(f => f.name.startsWith(initialFolder + '/')) ? initialFolder : '');
    const [selected, setSelected] = useState<string | null>(null);
    const [renamingPath, setRenamingPath] = useState<string | null>(null);
    const [renameValue, setRenameValue] = useState('');
    // The grid's fill for an internal drag; an OS file drag
    // lights the in-cell drop frame instead, naming the folder it lands in.
    const [isDragOver, setIsDragOver] = useState(false);
    const dropFrame = useInCellDropFrame();
    const [addDialog, setAddDialog] = useState<{ x: number; y: number; basePath: string; mode: 'file' | 'folder' } | null>(null);
    const [menu, setMenu] = useState<{ x: number; y: number; target: FileMenuTarget } | null>(null);

    const fullTree = useMemo(() => {
        const t = buildFileTree(files);
        if (diffTargetFiles) augmentTreeWithDeleted(t, diffTargetFiles, new Set(files.map(f => f.name)));
        return t;
    }, [files, diffTargetFiles]);

    // Reset navigation if the current folder vanished (deleted/renamed/moved).
    useEffect(() => {
        if (currentPath && childrenAt(fullTree, currentPath) === null) setCurrentPath('');
    }, [fullTree, currentPath]);

    // ── Report current folder + selection upward ───────────────────────────
    // THIS pane's current folder is the transfer panel's import target.
    // Callbacks ride on refs so the effects fire only when the folder/selection
    // actually change — not on every render (the parent passes fresh inline
    // closures, which would otherwise loop through the parent's setState).
    const onCurrentFolderChangeRef = useRef(onCurrentFolderChange);
    onCurrentFolderChangeRef.current = onCurrentFolderChange;
    const onSelectionChangeRef = useRef(onSelectionChange);
    onSelectionChangeRef.current = onSelectionChange;
    useEffect(() => { onCurrentFolderChangeRef.current?.(currentPath); }, [currentPath]);
    useEffect(() => {
        const cb = onSelectionChangeRef.current;
        if (!cb) return;
        if (!selected) { cb(null); return; }
        const node = findNode(fullTree, selected);
        cb(node ? { path: selected, isFolder: node.type === 'folder' } : null);
    }, [selected, fullTree]);

    const entries = useMemo(() => {
        const level = childrenAt(fullTree, currentPath) ?? {};
        return Object.values(level).sort(treeNodeOrder);
    }, [fullTree, currentPath]);

    const { handleCreateFileAtPath, handleCreateFolder, handleDuplicate, handleRename, handleDelete, handleDropFiles } =
        useFileOperations({ files, onUpdateFiles, readOnly, lockOwnerOf, channel, cellId });
    const fileTransfer = useFileTransfer({ cellId: cellId ?? '', channel, files, onUpdateFiles, readOnly, lockOwnerOf });
    const transferEnabled = !!cellId;
    const containerRef = useRef<HTMLDivElement>(null);
    // A paste lands in the folder on show.
    const paste = useFilesPanePaste({
        containerRef, files, cellId, readOnly, onUpdateFiles, onOpenFile, pasteInto: fileTransfer.pasteInto,
        targetFolder: () => currentPath,
    });

    const openAdd = (mode: 'file' | 'folder', basePath: string, anchor?: { left: number; bottom: number }) => {
        setAddDialog({ mode, basePath, x: anchor ? anchor.left : 120, y: anchor ? anchor.bottom + 4 : 80 });
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

    // Double-click: a folder is entered; a file opens as a NEW content tab
    // (a single click only selects here, so nothing to defer).
    const enter = (node: TreeNode) => {
        if (node.type === 'folder') setCurrentPath(node.path);
        else if (node.fileData) (onOpenFileInNewTab ?? onOpenFile)(node.fileData.name);
    };

    const startRename = (node: TreeNode) => { setRenamingPath(node.path); setRenameValue(node.name); };
    const commitRename = (node: TreeNode) => {
        if (renameValue.trim() && renameValue !== node.name) handleRename(node, renameValue.trim());
        setRenamingPath(null);
    };

    // Only a run writes under `_runs/`: its folders take no new file, no
    // paste and no drop.
    const inRunFolder = isRunPath(currentPath);

    // Drop onto a folder tile / the empty grid.
    const dropInto = (targetFolder: string, e: React.DragEvent) => {
        setIsDragOver(false);
        // Consume a drop into `_runs/` so neither the browser default (an OS
        // file would open in the tab) nor an ancestor's drop handler runs.
        if (isRunPath(targetFolder)) { e.preventDefault(); e.stopPropagation(); return; }
        if (fileTransfer.dropInto(targetFolder, e)) return;
        if (readOnly) return;
        e.preventDefault(); e.stopPropagation();
        // Snapshot synchronously — dataTransfer.items (the only view into a
        // dropped FOLDER) is gone the moment this handler returns.
        const capture = captureDrop(e.dataTransfer);
        // Floated on purpose (a drop handler can't await); the catch is the
        // last-resort guard so a failed import can't become a silent unhandled
        // rejection. handleDropFiles skips the individual files it can't take
        // and reports them itself, so reaching this catch means a real bug.
        if (hasDroppedItems(capture)) {
            void handleDropFiles(capture, targetFolder)
                .catch(err => console.error('[drop] importing the dropped files failed', err));
        }
    };
    // Own a drag without accepting it: the cursor shows no-drop, no drop
    // fires, the browser default (an OS file would open in the tab) never
    // runs, and no ancestor sees it (the canvas's NEW CELL slot stays
    // down). The in-cell drop frame goes dark.
    const refuseDrag = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = 'none';
        dropFrame.aim(e, null);
    };
    /** `targetFolder`: the folder tile under the drag, else the current folder. */
    const dragOver = (e: React.DragEvent, targetFolder = currentPath) => {
        // A run folder takes no drop.
        if (inRunFolder) { refuseDrag(e); return; }
        if (fileTransfer.isInternalDrag(e)) {
            // Stop here so the canvas's own dragover (cross-cell rule) can't
            // rewrite the effect this cell's drop will actually perform.
            e.stopPropagation();
            if (readOnly) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = fileTransfer.dropEffectFor(e);
            setIsDragOver(true);
            return;
        }
        if (readOnly) return; // don't signal a droppable target we'll ignore
        e.preventDefault(); e.stopPropagation();
        if (carriesOsFiles(e.dataTransfer)) dropFrame.aim(e, targetFolder);
        else setIsDragOver(true);
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if (isTypingTarget(e.nativeEvent)) return;
        const selNode = selected ? entries.find(n => n.path === selected) ?? null : null;
        const selLocked = !!selNode && (isRunPath(selNode.path)||!!nodeLockOwner(selNode, lockOwnerOf));
        const mod = e.ctrlKey || e.metaKey;
        if (e.key === 'Backspace' && !mod) { e.preventDefault(); if (currentPath) setCurrentPath(parentOf(currentPath)); return; }
        if (e.key === 'Enter' && selNode) { e.preventDefault(); enter(selNode); return; }
        if (e.key === 'F2' && selNode && !readOnly && !selLocked) { e.preventDefault(); startRename(selNode); return; }
        if ((e.key === 'Delete' || (e.key === 'Backspace' && mod)) && selNode && !readOnly && !selLocked) { e.preventDefault(); handleDelete(selNode); return; }
        if (!mod) return;
        const k = e.key.toLowerCase();
        // Mod+V is left to the browser: its `paste` event reaches
        // useFilesPanePaste with the OS clipboard's contents.
        if (!transferEnabled) return;
        if (selNode && k === 'c') { e.preventDefault(); fileTransfer.copyNode(selNode); }
        else if (selNode && k === 'x') { e.preventDefault(); fileTransfer.cutNode(selNode); }
    };

    const breadcrumb = ['', ...currentPath.split('/').filter(Boolean).map((_, i, arr) => arr.slice(0, i + 1).join('/'))];

    return (
        <div
            ref={containerRef}
            className="relative flex flex-col h-full bg-transparent outline-none"
            tabIndex={-1}
            onKeyDown={onKeyDown}
            onDragOver={(e) => dragOver(e)}
            {...dropFrame.frameProps}
            onDragLeave={(e) => { if (e.currentTarget.contains(e.relatedTarget as Node | null)) return; setIsDragOver(false); }}
            onDrop={(e) => dropInto(currentPath, e)}
            onContextMenu={(e) => {
                // A rename field keeps the browser's own menu (isEditableTarget).
                if (!transferEnabled || readOnly || isEditableTarget(e.target)) return;
                e.preventDefault();
                setMenu({ x: e.clientX, y: e.clientY, target: { kind: 'empty', basePath: currentPath } });
            }}
            onClick={() => setSelected(null)}
        >
            {/* Compact, icon-only toolbar — same reasoning as the tree's: this
                surface lives in a ~180px sidebar, so no title, no labels. */}
            <div className="flex items-center gap-0.5 px-1.5 h-[1.875rem] flex-shrink-0 border-b border-line bg-sheet select-none overflow-hidden">
                {!readOnly && !inRunFolder && (
                    <>
                        <button type="button" title="New file" onClick={(e) => { e.stopPropagation(); openAdd('file', currentPath, e.currentTarget.getBoundingClientRect()); }} className={RAIL_BUTTON}>
                            <SquarePlus className="w-3.5 h-3.5" />
                        </button>
                        <button type="button" title="New folder" onClick={(e) => { e.stopPropagation(); openAdd('folder', currentPath, e.currentTarget.getBoundingClientRect()); }} className={RAIL_BUTTON}>
                            <FolderPlus className="w-3.5 h-3.5" />
                        </button>
                    </>
                )}
                <div className="flex-1 min-w-0" />
            </div>

            {/* Breadcrumb */}
            <div className="flex items-center gap-0.5 px-3 py-1.5 border-b border-line text-xs flex-shrink-0 overflow-x-auto">
                {currentPath && (
                    <button type="button" title="Up" onClick={(e) => { e.stopPropagation(); setCurrentPath(parentOf(currentPath)); }} className={cn(iconBtn, 'w-[1.375rem] h-[1.375rem] mr-1')}>
                        <ArrowUp className="w-3.5 h-3.5" />
                    </button>
                )}
                {breadcrumb.map((p, i) => (
                    <React.Fragment key={p || 'root'}>
                        {i > 0 && <ChevronRight className="w-3 h-3 text-ink-3" />}
                        <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); setCurrentPath(p); }}
                            className={cn('px-1 py-0.5 rounded-xs hover:bg-sheet-2 truncate max-w-[8.75rem]', p === currentPath ? 'text-ink' : 'text-ink-2 hover:text-ink')}
                        >
                            {i === 0 ? 'root' : p.split('/').pop()}
                        </button>
                    </React.Fragment>
                ))}
            </div>

            {/* Grid */}
            {cellId&&!readOnly&&<FileConflictNotice channel={channel||currentChannelId()} cellId={cellId} onOpenFile={onOpenFile}/>}
            <ScrollArea className="flex-1">
                {/* minmax(80px) + p-2 keeps TWO columns at the sidebar's
                    ~180px working width — one column reads as a bad list. */}
                <div className={cn('p-2 grid grid-cols-[repeat(auto-fill,minmax(5rem,1fr))] gap-1.5 min-h-[7.5rem]', isDragOver && 'bg-teal-soft')}>
                    {entries.length === 0 && (
                        <div className="col-span-full text-center py-4 text-xs text-ink-3 italic border border-dashed border-line-2 rounded-md">
                            Empty folder — drop files here
                        </div>
                    )}
                    {entries.map((node) => {
                        const isFolder = node.type === 'folder';
                        const isDeleted = !!node.isDeleted;
                        const diff = fileDiffStatus(node, diffTargetFiles);
                        const isSel = selected === node.path;
                        const filePath = node.fileData?.name;
                        const isActiveTab = !!filePath && activePath === filePath;
                        const isPinned = node.type === 'file' && !!node.fileData && pinnedName === node.fileData.name;
                        const isRenaming = renamingPath === node.path;
                        const lockOwner = nodeLockOwner(node, lockOwnerOf);
                        return (
                            <div
                                key={node.path}
                                role="button"
                                tabIndex={isDeleted ? -1 : 0}
                                draggable={transferEnabled && !isDeleted && !isRenaming}
                                onDragStart={isDeleted ? undefined : (e) => { e.stopPropagation(); fileTransfer.startDrag(node, e); }}
                                onDragEnd={isDeleted ? undefined : () => fileTransfer.endDrag()}
                                // A folder tile takes the drag into its folder or refuses it: a
                                // `_runs` tile, or one the view would not drop into (a read-only
                                // view), never hands it to the grid or the canvas. A refusal also
                                // turns off the grid highlight, which a crossing into a tile no
                                // longer clears.
                                onDragOver={isFolder && !isDeleted ? (e) => {
                                    e.stopPropagation();
                                    if (!isRunPath(node.path)) dragOver(e, node.path);
                                    if (!e.isDefaultPrevented()) { refuseDrag(e); setIsDragOver(false); }
                                } : undefined}
                                onDrop={isFolder && !isDeleted ? (e) => { e.stopPropagation(); dropInto(node.path, e); } : undefined}
                                onClick={(e) => { e.stopPropagation(); if (!isDeleted && !isRenaming) setSelected(node.path); }}
                                onDoubleClick={(e) => { e.stopPropagation(); if (!isDeleted && !isRenaming) enter(node); }}
                                onFocus={() => { if (!isDeleted) setSelected(node.path); }}
                                onContextMenu={isDeleted ? undefined : (e) => { if (isEditableTarget(e.target)) return; e.preventDefault(); e.stopPropagation(); setSelected(node.path); setMenu({ x: e.clientX, y: e.clientY, target: { kind: node.type, node } as FileMenuTarget }); }}
                                title={lockOwner ? `${node.path} — ${lockTitle(lockOwner)}` : node.path}
                                className={cn(
                                    'relative flex flex-col items-center gap-1 p-2 rounded-md border transition-colors select-none outline-none',
                                    isDeleted ? 'opacity-50 border-transparent' : 'cursor-pointer',
                                    isSel ? 'bg-brass-soft border-brass' : 'border-transparent hover:bg-sheet-2 focus:bg-sheet-2',
                                )}
                            >
                                <div className="relative">
                                    {isFolder
                                        ? <Folder className="w-9 h-9 text-ink-3" />
                                        : <div className="scale-150">{getFileIcon(node.fileData?.language || 'text', node.name)}</div>}
                                    {isPinned && <Pin className="absolute -bottom-1 -right-1 w-3 h-3 text-teal fill-teal/70" />}
                                    {lockOwner && <Lock data-testid="file-lock" className="absolute -bottom-1 -left-1 w-3 h-3 text-ink-3" aria-label={lockTitle(lockOwner)} />}
                                    {diff && (
                                        <span className={cn('absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full border border-sheet',
                                            diff === 'added' ? 'bg-green' : diff === 'modified' ? 'bg-ink-2' : 'bg-red')} />
                                    )}
                                </div>
                                {isRenaming ? (
                                    <Input
                                        value={renameValue}
                                        autoFocus
                                        onClick={(e) => e.stopPropagation()}
                                        onChange={(e) => setRenameValue(e.target.value)}
                                        onBlur={() => commitRename(node)}
                                        onKeyDown={(e) => { e.stopPropagation(); if (e.key === 'Enter') commitRename(node); if (e.key === 'Escape') setRenamingPath(null); }}
                                        className="h-5 py-0 px-1 rounded-xs bg-sheet border-line-2 text-ink text-[0.6875rem] text-center shadow-none focus-visible:ring-teal"
                                    />
                                ) : (
                                    <span className={cn('text-[0.6875rem] text-center leading-tight line-clamp-2 break-all', isActiveTab ? 'text-teal' : isSel ? 'text-ink' : 'text-ink-2', isDeleted && 'line-through')}>
                                        {node.name}
                                    </span>
                                )}
                            </div>
                        );
                    })}
                </div>
            </ScrollArea>

            {dropFrame.target !== null && <InCellDropFrame cellName={dropLabel} folder={dropFrame.target} />}

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
                <CellFileContextMenu menu={menu} readOnly={readOnly} locked={menu.target.kind !== 'empty' && (isRunPath(menu.target.node.path)||!!nodeLockOwner(menu.target.node, lockOwnerOf))} transfer={fileTransfer} pasteHere={transferEnabled ? paste.pasteHere : undefined} cellId={cellId} pinnedName={pinnedName} onClose={()=>setMenu(null)} onOpen={menu.target.kind !== 'empty' ? () => enter((menu.target as {node:TreeNode}).node) : undefined} onRename={menu.target.kind !== 'empty' ? () => startRename((menu.target as {node:TreeNode}).node) : undefined} onOpenFileWith={onOpenFileWith} onDelete={handleDelete} onDuplicate={handleDuplicate} onTogglePin={onTogglePin} onNewFile={base=>openAdd('file',base,{left:menu.x,bottom:menu.y})} onNewFolder={base=>openAdd('folder',base,{left:menu.x,bottom:menu.y})} />
            )}
        </div>
    );
};
