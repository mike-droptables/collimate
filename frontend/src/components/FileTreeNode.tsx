import { useEffect, useMemo, useRef, useState } from 'react';
import { Folder, FolderOpen, Pen, Trash2, Plus, Pin, Lock } from 'lucide-react';
import { getFileIcon } from './fileIcon';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { TreeNode, computeLineStats, fileDiffStatus } from '@/lib/fileTree';
import { treeNodeOrder } from '@/lib/fileSearch';
import { isRunPath } from '@/lib/runningCell';
import { lockTitle, nodeLockOwner, type LockOwnerOf } from '@/lib/fileLocks';
import type { RunRecord } from '@/lib/types';
import { captureDrop, hasDroppedItems, type DropCapture } from '@/lib/dropEntries';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { isEditableTarget } from '@/lib/nativeSelection';
import {useCollimateStore} from '@/stores/useCollimateStore';


/** How long a single click's open waits for a possible second click. Just
 *  past the platform double-click threshold most people actually hit. */
const DBLCLICK_WINDOW_MS = 220;

/** Broadcast collapse/expand signal from FileBrowser header buttons
 *  down to every FileTreeNode. Changing nonce triggers a one-shot
 *  effect in each node that snaps its `isOpen` to the target state;
 *  after that, per-node local state takes over again. Nonce 0 means
 *  "no broadcast yet" — nodes keep their default open state. */
export interface CollapseSignal {
    nonce: number;
    collapsed: boolean;
}

/** Cross-cell transfer + context-menu wiring, supplied by FileBrowser (which
 *  owns the useFileTransfer hook and the context-menu state). Threaded down to
 *  every row so files/folders are draggable, droppable, and right-clickable. */
export interface FileRowTransfer {
    onContextMenu: (node: TreeNode, x: number, y: number) => void;
    onDragStartNode: (node: TreeNode, e: React.DragEvent) => void;
    onDragEndNode: () => void;
    isInternalDrag: (e: React.DragEvent) => boolean;
    dropEffectFor: (e: React.DragEvent) => 'copy' | 'move';
    /** Apply an internal drop into `targetFolder`. Returns true if it was an
     *  internal transfer (handled) — false means fall through to OS-file drop. */
    onDropInto: (targetFolder: string, e: React.DragEvent) => boolean;
    /** When equal to a row's path, that row enters inline-rename (driven by
     *  the context menu's Rename action). */
    renamePath?: string | null;
    /** A row calls this after it has consumed `renamePath`. */
    onRenameConsumed?: () => void;
}

interface FileTreeSharedProps {
    /** Open a file by PATH. Tabs are path-keyed; the array index a node
     *  carries is an internal detail of the rename/delete operations only. */
    onOpenFile: (path: string) => void;
    /** Double-click: open the file as a NEW content tab (the single-click
     *  open takes the active tab over). Absent = double-click is a click. */
    onOpenFileInNewTab?: (path: string) => void;
    /** Path of the focused content-pane tab, if it is a file tab. */
    activePath?: string | null;
    /** Report the focused/clicked row upward (the transfer panel's import
     *  target). Called from event handlers, never an effect. */
    onSelectionChange?: (sel: { path: string; isFolder: boolean }) => void;
    // `anchor` (a button/cursor rect) positions the New-file popover at the
    // invoking element; omitted callers fall back to a default position.
    onCreateFile: (path: string, anchor?: { left: number; bottom: number }) => void;
    onRename: (node: TreeNode, newName: string) => void | Promise<void>;
    onDelete: (node: TreeNode) => void;
    /** Path of the file currently pinned as the cell's preview, if any. */
    pinnedName?: string;
    /** Toggle a file's pinned state. Absent → no pin affordance (e.g. read-only). */
    onTogglePin?: (fileName: string) => void;
    readOnly: boolean;
    /** The live run owning a path (lib/fileLocks). A locked file — and a
     *  folder holding one — shows a lock with its owner, and offers no
     *  delete, rename or move (a drag copies). Absent = nothing is locked. */
    lockOwnerOf?: LockOwnerOf;
    /** OS-file drop onto a folder row. Takes the synchronous DropCapture (not
     *  a FileList) so a dropped FOLDER can be walked recursively after the
     *  event — see lib/dropEntries.
     *
     *  Void-returning by contract: the import is asynchronous and a drop
     *  handler can't await it, so the HANDLER owns its own failures (see
     *  FileBrowser's `dropFilesInto`) — a row can't report what it never
     *  learns about. */
    onDropFiles: (capture: DropCapture, targetPath: string) => void;
    /** Cross-cell transfer + context-menu wiring (optional; absent in
     *  contexts that don't support it, e.g. some read-only/diff embeds). */
    transfer?: FileRowTransfer;
    diffTargetFiles?: Record<string, string>;
    /** Set by FileBrowser's header "Expand all" / "Collapse all"
     *  buttons. Every FileTreeNode re-snaps its local isOpen when
     *  nonce changes. Optional — when absent, nodes manage their
     *  own state entirely. */
    collapseSignal?: CollapseSignal;
}

interface FileTreeProps extends FileTreeSharedProps {
    nodes: Record<string, TreeNode>;
    level: number;
}

export const FileTree = ({ nodes, level, ...shared }: FileTreeProps) => {
    const sortedNodes = Object.values(nodes).sort(treeNodeOrder);

    // Rows touch (no gap): the pointer passes straight from one row to the
    // next, so the hover highlight hands over in one frame instead of
    // blinking off across a dead strip between them.
    return (
        <div className="flex flex-col">
            {sortedNodes.map((node) => (
                <FileTreeNode
                    key={node.path}
                    node={node}
                    level={level}
                    {...shared}
                />
            ))}
        </div>
    );
};

interface FileTreeNodeProps extends FileTreeSharedProps {
    node: TreeNode;
    level: number;
}

/* Row hover is INSTANT on purpose: the background, text colours and the
 * hover-only actions all switch in the same frame, so exactly one row is ever
 * lit. A fade (it was 150 ms) left the row just left still lit while the new
 * row's actions had already appeared — two rows showing hover at once, read
 * as the highlight jumping between them. Only the press scale animates. */

/** A tree row (the mock's `.tree .row`): 24px, 12.5px type, a 6px gap, 6px
 *  of padding plus 12px per level, ink-2 on a sheet-2 hover. */
const ROW = 'group/file-row flex items-center gap-1.5 h-6 pr-1.5 text-[0.78125rem] rounded-xs select-none outline-none relative transition-[scale] duration-150 motion-reduce:transition-none focus-visible:ring-1 focus-visible:ring-teal';
const rowIndent = (level: number) => ({ paddingLeft: `${level * 12 + 6}px` });
/** A row's icon: 14px, ink-3. A file's kind glyph comes from getFileIcon,
 *  sized down here. */
const ROW_ICON = 'flex-none inline-flex text-ink-3 [&>svg]:w-3.5 [&>svg]:h-3.5';
/** The file's extension after its name: mono, ink-3 (ink-2 on the open file). */
const EXT = 'flex-none font-mono text-[0.6875rem]';

/** `frame.md` → `frame` + `.md`; a dotfile is all extension. */
function splitName(name: string): { stem: string; ext: string } {
    const dot = name.lastIndexOf('.');
    return dot < 0 ? { stem: name, ext: '' } : { stem: name.slice(0, dot), ext: name.slice(dot) };
}

/** A row's hover tool (rename, pin): ink-3, a pressed fill over the row's hover fill. */
const ROW_TOOL = 'p-0.5 rounded-xs cursor-pointer text-ink-3 hover:bg-sheet-3 hover:text-ink';
/** A row's delete tool: red with the soft red fill on hover. */
const ROW_TOOL_DANGER = 'p-0.5 rounded-xs cursor-pointer text-ink-3 hover:bg-red-soft hover:text-red';
/** A diff-status letter (A / M / D) after a name. */
const DIFF_BADGE = 'ml-1 flex-none px-1 py-0.5 rounded-xs border text-[0.5625rem] font-bold leading-none';
const DIFF_ADDED = cn(DIFF_BADGE, 'bg-green/15 border-green/30 text-green');
const DIFF_DELETED = cn(DIFF_BADGE, 'bg-red-soft border-red/30 text-red');
/** The inline rename field. */
const RENAME_FIELD = 'h-5 py-0 px-1 text-xs rounded-xs bg-sheet border-line-2 text-ink shadow-none focus-visible:ring-teal';


function LockGlyph({ owner }: { owner: RunRecord }) {
    return (
        <span className="flex-none text-ink-3" title={lockTitle(owner)} data-testid="file-lock">
            <Lock className="w-3 h-3" aria-label={lockTitle(owner)} />
        </span>
    );
}

export const FileTreeNode = ({ node, level, ...shared }: FileTreeNodeProps) => {
    const {
        onOpenFile, onOpenFileInNewTab, activePath, onSelectionChange,
        onCreateFile, onRename, onDelete, pinnedName, onTogglePin,
        readOnly, lockOwnerOf, onDropFiles, transfer, diffTargetFiles,
        collapseSignal,
    } = shared;
    // Initial open state honors the latest collapseSignal, so a node
    // that mounts AFTER a "Collapse all" broadcast (e.g. the nested
    // children of a folder the user then expands) comes up collapsed
    // instead of wide open. Once mounted, local state takes over.
    const [isOpen, setIsOpen] = useState(
        collapseSignal && collapseSignal.nonce !== 0 ? !collapseSignal.collapsed : true,
    );
    const [isRenaming, setIsRenaming] = useState(false);
    const [renameValue, setRenameValue] = useState(node.name);
    const [isDragOver, setIsDragOver] = useState(false);
    // The deferred single-click open of a FILE row (see its onClick) —
    // cleared by a double-click, or on unmount so a row removed mid-window
    // can't open a file. Declared up here, unconditionally, ahead of the
    // folder/file branch below.
    const clickOpenRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEffect(() => () => { if (clickOpenRef.current) clearTimeout(clickOpenRef.current); }, []);

    // Snap this node's open state to the broadcast target whenever
    // FileBrowser's header buttons bump the collapseSignal nonce.
    // Nonce of 0 is the "never broadcast" sentinel — ignore it.
    useEffect(() => {
        if (!collapseSignal || collapseSignal.nonce === 0) return;
        setIsOpen(!collapseSignal.collapsed);
    }, [collapseSignal?.nonce]);  // eslint-disable-line react-hooks/exhaustive-deps

    // Enter inline-rename when the context menu targets this row.
    useEffect(() => {
        if (transfer?.renamePath && transfer.renamePath === node.path) {
            setIsRenaming(true);
            transfer.onRenameConsumed?.();
        }
    }, [transfer?.renamePath, node.path]);  // eslint-disable-line react-hooks/exhaustive-deps

    // The pin is teal, brass in the selected cell (brass marks the selection).
    const selectedCell = useCellSelected();
    const lockOwner = nodeLockOwner(node, lockOwnerOf);
    const locked = isRunPath(node.path)||!!lockOwner;
    // Only a run writes under `_runs/`: a folder there takes no new file
    // and no drop.
    const inRunFolder = node.type === 'folder' && isRunPath(node.path);

    const handleSaveRename = () => {
        if (renameValue.trim() && renameValue !== node.name) {
            void Promise.resolve().then(()=>onRename(node, renameValue.trim())).catch(error=>
                useCollimateStore.getState().openAlert({title:'Rename failed',body:String(error?.message??error)}));
        } else {
            setRenameValue(node.name);
        }
        setIsRenaming(false);
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter') {
            // Renaming may focus a confirmation button synchronously. Consume
            // this Enter so its native click cannot also approve that dialog.
            e.preventDefault();
            handleSaveRename();
        }
        if (e.key === 'Escape') { setRenameValue(node.name); setIsRenaming(false); }
        e.stopPropagation();
    };

    const handleDragOver = (e: React.DragEvent) => {
        if (node.type !== 'folder') return;
        // Nothing is dropped under `_runs/`: swallow the dragover without
        // preventDefault so the tree root can't accept it on this row's behalf.
        if (inRunFolder) { e.stopPropagation(); return; }
        if (transfer?.isInternalDrag(e)) {
            // Internal cell-to-cell transfer. Can't drop into a read-only cell.
            if (readOnly) return;
            e.preventDefault(); e.stopPropagation();
            e.dataTransfer.dropEffect = transfer.dropEffectFor(e);
            setIsDragOver(true);
            return;
        }
        if (readOnly) return;
        e.preventDefault(); e.stopPropagation();
        setIsDragOver(true);
    };

    const handleDragLeave = (e: React.DragEvent) => {
        if (node.type !== 'folder') return;
        e.preventDefault(); e.stopPropagation();
        // Moving onto the row's own chevron / icon / name is not a leave.
        if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
        setIsDragOver(false);
    };

    const handleDrop = (e: React.DragEvent) => {
        if (node.type !== 'folder') return;
        // Refuse (not redirect to the root) a drop that still reaches a `_runs` row.
        if (inRunFolder) { e.preventDefault(); e.stopPropagation(); setIsDragOver(false); return; }
        setIsDragOver(false);
        // Internal transfer first (handles its own preventDefault/stopPropagation);
        // fall through to the OS-file import only when it wasn't ours.
        if (transfer?.onDropInto(node.path, e)) { setIsOpen(true); return; }
        if (readOnly) return;
        e.preventDefault(); e.stopPropagation();
        // Synchronous snapshot: a dropped FOLDER lives only in
        // dataTransfer.items, which the browser empties as this handler
        // returns (see lib/dropEntries).
        const capture = captureDrop(e.dataTransfer);
        if (hasDroppedItems(capture)) {
            onDropFiles(capture, node.path);
            setIsOpen(true);
        }
    };

    // Diff badges depend only on this node and diffTargetFiles, so they are
    // memoised (above the folder/file branch, as hooks must be): a re-render
    // of the tree neither rescans the target paths nor re-splits contents.
    const folderIsAdded = useMemo(
        () => node.type === 'folder' && !node.isDeleted && !!diffTargetFiles
            && !Object.keys(diffTargetFiles).some(k => k.startsWith(node.path + '/')),
        [node.type, node.isDeleted, node.path, diffTargetFiles],
    );
    const diffFile = node.type === 'file' && !node.isDeleted ? node.fileData : undefined;
    const diffTargetContent = diffFile && diffTargetFiles ? diffTargetFiles[diffFile.name] : undefined;
    const diffFileContent = (diffFile && 'content' in diffFile ? String(diffFile.content) : undefined);
    const modifiedStats = useMemo(
        () => diffTargetContent !== undefined && diffFileContent !== undefined && diffTargetContent !== diffFileContent
            ? computeLineStats(diffTargetContent, diffFileContent)
            : null,
        [diffTargetContent, diffFileContent],
    );

    if (node.type === 'folder') {
        // Folder-level diff status:
        //  - deleted: virtual folder for a path that exists only in the
        //    diff target. Gray-and-strike, no create/rename/drag.
        //  - added: folder exists in current but NONE of its files are
        //    in the diff target — the whole subtree is new.
        // Otherwise no badge (mixed content is signalled by file-level
        // A/M/D badges inside).
        const folderIsDeleted = !!node.isDeleted;

        return (
            <div>
                <div
                    role="button"
                    data-filenode-path={node.path}
                    data-filenode-kind="folder"
                    tabIndex={folderIsDeleted ? -1 : 0}
                    // Keyboard nav — Enter toggles expand (also read-only
                    // or locked, like a click), F2 renames, Del/Backspace
                    // deletes. Scoped to this row's focus; no global
                    // shortcut registration needed because the handlers
                    // only fire when a row has focus.
                    onKeyDown={(e) => {
                        if (folderIsDeleted || isRenaming) return;
                        if (e.key === 'Enter') { e.preventDefault(); setIsOpen(v => !v); }
                        else if (readOnly || locked) return;
                        else if (e.key === 'F2') { e.preventDefault(); setIsRenaming(true); }
                        else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); onDelete(node); }
                    }}
                    onFocus={folderIsDeleted ? undefined : () => onSelectionChange?.({ path: node.path, isFolder: true })}
                    // The WHOLE row toggles the folder and shows the hand
                    // cursor, like a file row. When only an inner box did,
                    // the row's padding showed the arrow, so moving between
                    // folders flipped the cursor hand → arrow → hand.
                    onClick={() => {
                        if (!folderIsDeleted) onSelectionChange?.({ path: node.path, isFolder: true });
                        setIsOpen(!isOpen);
                    }}
                    className={cn(
                        ROW, 'cursor-pointer',
                        folderIsDeleted
                            ? 'opacity-60 text-ink-2'
                            : isDragOver ? 'bg-teal-soft ring-1 ring-teal text-ink' : 'text-ink-2 hover:bg-sheet-2 active:bg-sheet-3 motion-safe:active:scale-[0.99]',
                    )}
                    style={rowIndent(level)}
                    draggable={!!transfer && !folderIsDeleted && !isRenaming}
                    onDragStart={folderIsDeleted ? undefined : (e) => transfer?.onDragStartNode(node, e)}
                    onDragEnd={folderIsDeleted ? undefined : () => transfer?.onDragEndNode()}
                    onDragOver={folderIsDeleted ? undefined : handleDragOver}
                    onDragLeave={folderIsDeleted ? undefined : handleDragLeave}
                    onDrop={folderIsDeleted ? undefined : handleDrop}
                    onContextMenu={folderIsDeleted ? undefined : (e) => { if (isEditableTarget(e.target)) return; e.preventDefault(); e.stopPropagation(); transfer?.onContextMenu(node, e.clientX, e.clientY); }}
                    title={folderIsDeleted ? 'Deleted — this folder exists in the diff target but not in the current cell.' : undefined}
                >
                    <div className="flex items-center gap-1.5 flex-1 min-w-0 overflow-hidden h-full">
                        {/* The folder's glyph says whether it is open (the
                            mock's rows carry no chevron). */}
                        <span className={ROW_ICON}>{isOpen ? <FolderOpen /> : <Folder />}</span>

                        {isRenaming ? (
                            <Input
                                value={renameValue}
                                onChange={(e) => setRenameValue(e.target.value)}
                                onBlur={handleSaveRename}
                                onKeyDown={handleKeyDown}
                                onClick={(e) => e.stopPropagation()}
                                autoFocus
                                className={RENAME_FIELD}
                            />
                        ) : (
                            <div className="flex items-center gap-1.5 flex-1 min-w-0">
                                <span className={cn('truncate min-w-0', folderIsDeleted && 'line-through')}>{node.name}</span>
                                {lockOwner && <LockGlyph owner={lockOwner} />}
                                {/* Row actions show on hover and never shrink:
                                    the name truncates to make room, so they
                                    stay inside the row and the pane. */}
                                {!readOnly && !folderIsDeleted && !locked && (
                                    <div className="hidden group-hover/file-row:flex items-center gap-0.5 flex-none">
                                        <div role="button" title="Rename" onClick={(e) => { e.stopPropagation(); setIsRenaming(true); }} className={ROW_TOOL}>
                                            <Pen className="w-3.5 h-3.5" />
                                        </div>
                                        <div role="button" title="Delete" onClick={(e) => { e.stopPropagation(); onDelete(node); }} className={ROW_TOOL_DANGER}>
                                            <Trash2 className="w-3.5 h-3.5" />
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>

                    {folderIsAdded && (
                        <div className={DIFF_ADDED} title="Added folder">A</div>
                    )}
                    {folderIsDeleted && (
                        <div className={DIFF_DELETED} title="Deleted folder">D</div>
                    )}
                    {!readOnly && !folderIsDeleted && !inRunFolder && (
                        // Single "+" per folder — opens the shared inline
                        // popup anchored to the header. "Add folder" is
                        // gone; folders emerge from filename paths.
                        <div className="hidden group-hover/file-row:flex items-center gap-1 flex-none ml-auto">
                            <div role="button" title="New file in this folder" onClick={(e) => { e.stopPropagation(); setIsOpen(true); onCreateFile(node.path, e.currentTarget.getBoundingClientRect()); }} className={ROW_TOOL}>
                                <Plus className="w-3.5 h-3.5" />
                            </div>
                        </div>
                    )}
                </div>
                {isOpen && (
                    <FileTree
                        nodes={node.children}
                        level={level + 1}
                        {...shared}
                    />
                )}
            </div>
        );
    }

    const filePath = node.fileData?.name ?? node.path;
    const isActiveTab = !!node.fileData && activePath === filePath;
    // Diff status for a file: added (only in current), modified
    // (present in both with different content), or deleted (virtual
    // node synthesised for a file only in the target). For modified
    // files we also compute cheap +N/-M line counts so the badge
    // reads "M +5/-2" instead of just "M".
    const diffStatus = fileDiffStatus(node, diffTargetFiles);
    const diffStats = diffStatus === 'modified' ? modifiedStats : null;

    // Deleted files are non-interactive placeholders — click does
    // nothing, rename/delete hover buttons are hidden, and the entire
    // row is muted so it doesn't pull focus from real files. Tooltip
    // explains what the row means.
    const isDeletedRow = diffStatus === 'deleted';

    // Pinned-as-preview state. Compared on the full path (node.fileData.name),
    // which is what cell.pinned stores. The indicator shows regardless of
    // onTogglePin (so read-only views still surface what's pinned); the
    // toggle control below is gated on onTogglePin.
    const isPinned = node.type === 'file' && !!node.fileData
        && pinnedName !== undefined && pinnedName === node.fileData.name;
    const pinTone = selectedCell ? 'text-brass' : 'text-teal';
    const { stem, ext } = splitName(node.name);

    return (
        <div
            role="button"
            data-filenode-path={node.path}
            data-filenode-kind="file"
            aria-current={isActiveTab ? 'true' : undefined}
            tabIndex={isDeletedRow ? -1 : 0}
            onFocus={isDeletedRow ? undefined : () => onSelectionChange?.({ path: filePath, isFolder: false })}
            onClick={() => {
                if (isDeletedRow || isRenaming) return;
                onSelectionChange?.({ path: filePath, isFolder: false });
                if (!node.fileData) return;
                // A double-click must open a NEW tab without the first click
                // having already taken the active tab over — so the single-
                // click open is deferred past the double-click window and
                // cancelled when the second click lands.
                if (!onOpenFileInNewTab) { onOpenFile(filePath); return; }
                if (clickOpenRef.current) clearTimeout(clickOpenRef.current);
                clickOpenRef.current = setTimeout(() => {
                    clickOpenRef.current = null;
                    onOpenFile(filePath);
                }, DBLCLICK_WINDOW_MS);
            }}
            onDoubleClick={onOpenFileInNewTab ? (e) => {
                e.stopPropagation();
                if (isDeletedRow || isRenaming || !node.fileData) return;
                if (clickOpenRef.current) { clearTimeout(clickOpenRef.current); clickOpenRef.current = null; }
                onOpenFileInNewTab(filePath);
            } : undefined}
            onKeyDown={(e) => {
                if (isDeletedRow || isRenaming) return;
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (node.fileData) onOpenFile(filePath);
                } else if (readOnly || locked) {
                    return;
                } else if (e.key === 'F2') {
                    e.preventDefault();
                    setIsRenaming(true);
                } else if (e.key === 'Delete' || e.key === 'Backspace') {
                    e.preventDefault();
                    onDelete(node);
                }
            }}
            title={isDeletedRow ? 'Deleted — this file exists in the diff target but not in the current cell.' : undefined}
            className={cn(
                ROW,
                isDeletedRow
                    ? 'opacity-60 cursor-default text-ink-3'
                    : 'cursor-pointer hover:bg-sheet-2 active:bg-sheet-3 motion-safe:active:scale-[0.99]',
                // The open file: sheet-2 in ink.
                !isDeletedRow && (isActiveTab ? 'bg-sheet-2 text-ink' : 'text-ink-2'),
            )}
            style={rowIndent(level)}
            draggable={!!transfer && !isDeletedRow && !isRenaming}
            onDragStart={isDeletedRow ? undefined : (e) => transfer?.onDragStartNode(node, e)}
            onDragEnd={isDeletedRow ? undefined : () => transfer?.onDragEndNode()}
            onContextMenu={isDeletedRow ? undefined : (e) => { if (isEditableTarget(e.target)) return; e.preventDefault(); e.stopPropagation(); transfer?.onContextMenu(node, e.clientX, e.clientY); }}
        >
            <div className="flex items-center gap-1.5 overflow-hidden flex-1 min-w-0">
                <span className={ROW_ICON}>{getFileIcon(node.fileData?.language || 'text', node.name)}</span>

                {isRenaming ? (
                    <Input
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onBlur={handleSaveRename}
                        onKeyDown={handleKeyDown}
                        onClick={(e) => e.stopPropagation()}
                        autoFocus
                        className={RENAME_FIELD}
                    />
                ) : (
                    <div className="flex items-center gap-1.5 min-w-0 flex-1">
                        {stem && <span className={cn('truncate min-w-0', isDeletedRow && 'line-through')}>{stem}</span>}
                        {ext && <span className={cn(EXT, isActiveTab && !isDeletedRow ? 'text-ink-2' : 'text-ink-3', isDeletedRow && 'line-through')}>{ext}</span>}
                        {lockOwner && <LockGlyph owner={lockOwner} />}
                        {/* One pin control: the pinned file's filled pin IS the
                            unpin button (always shown); an unpinned file offers
                            a pin among its hover actions, which pins it in
                            place of any other. Read-only views show the pin as
                            a plain marker. */}
                        {isPinned && (onTogglePin && !readOnly && !locked ? (
                            <div role="button" title="Unpin" aria-label="Unpin"
                                onClick={(e) => { e.stopPropagation(); onTogglePin(node.fileData!.name); }}
                                className={cn('ml-auto flex-none inline-flex rounded-xs cursor-pointer', pinTone, selectedCell ? 'hover:bg-brass-soft' : 'hover:bg-teal-soft')}>
                                <Pin className="w-3.5 h-3.5" />
                            </div>
                        ) : (
                            <Pin className={cn('ml-auto w-3.5 h-3.5 flex-none', pinTone)} aria-label="Pinned as preview" />
                        ))}
                        {diffStatus === 'added' && <span className={DIFF_ADDED} title="Added">A</span>}
                        {diffStatus === 'modified' && (
                            <span className={cn(DIFF_BADGE, 'inline-flex items-center gap-1 border-line-2 text-ink-2')}
                                title={`Modified — ${diffStats?.added ?? 0} added, ${diffStats?.removed ?? 0} removed`}>
                                M
                                {diffStats && (diffStats.added > 0 || diffStats.removed > 0) && (
                                    <span className="font-mono text-[0.5rem] opacity-80">
                                        <span className="text-green">+{diffStats.added}</span>
                                        <span className="mx-0.5 text-ink-3">/</span>
                                        <span className="text-red">-{diffStats.removed}</span>
                                    </span>
                                )}
                            </span>
                        )}
                        {diffStatus === 'deleted' && (
                            <span className={DIFF_DELETED} title="Deleted">D</span>
                        )}
                        {/* Row actions show on hover and never shrink: the
                            name truncates to make room, so pin / rename /
                            delete stay inside the row and the pane. */}
                        {/* A locked file offers none: the run's file is never
                            deleted, renamed or pinned (it vanishes with the
                            run, so it can't be the cell's preview). */}
                        {!readOnly && !isDeletedRow && !locked && (
                            <div className={cn('hidden group-hover/file-row:flex items-center gap-0.5 flex-none', !isPinned && 'ml-auto')}>
                                {onTogglePin && node.fileData && !isPinned && (
                                    <div role="button" title="Pin as preview" onClick={(e) => { e.stopPropagation(); onTogglePin(node.fileData!.name); }} className={cn('p-0.5 rounded-xs cursor-pointer text-ink-3 hover:bg-sheet-3', selectedCell ? 'hover:text-brass' : 'hover:text-teal')}>
                                        <Pin className="w-3.5 h-3.5" />
                                    </div>
                                )}
                                <div role="button" title="Rename" onClick={(e) => { e.stopPropagation(); setIsRenaming(true); }} className={ROW_TOOL}>
                                    <Pen className="w-3.5 h-3.5" />
                                </div>
                                <div role="button" title="Delete" onClick={(e) => { e.stopPropagation(); onDelete(node); }} className={ROW_TOOL_DANGER}>
                                    <Trash2 className="w-3.5 h-3.5" />
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};
