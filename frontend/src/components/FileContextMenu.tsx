import { useViewportClampedPosition, useDismissOnOutside } from '@/hooks/usePopoverPosition';
import React, { useRef } from 'react';
import { createPortal } from 'react-dom';
import {
    FolderOpen, FilePlus, FolderPlus, Pen, Trash2, Scissors, Copy,
    ClipboardPaste, CopyPlus, Pin, PinOff, ChevronsUpDown, ChevronsDownUp,
} from 'lucide-react';
import { TreeNode } from '@/lib/fileTree';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { OpenWithSubmenu, hasOpenWithChoices } from '@/components/OpenWithSubmenu';
import { CompareWithSubmenu } from '@/components/CompareWithSubmenu';
import { menuItem, menuItemDanger, menuSurface } from '@/components/ui/menuStyles';
import { isRunPath } from '@/lib/runningCell';

export type FileMenuTarget =
    | { kind: 'file'; node: TreeNode }
    | { kind: 'folder'; node: TreeNode }
    | { kind: 'empty'; basePath: string };

interface FileContextMenuProps {
    x: number;
    y: number;
    target: FileMenuTarget;
    readOnly: boolean;
    /** The target is a live run's locked file, or a folder holding one: it
     *  can be copied and duplicated (in the cell, outside `_runs/`), never
     *  cut, renamed, deleted or pinned. */
    locked?: boolean;
    /** The internal clipboard's label ("'components'"), null when it is
     *  empty: it names the Paste item. */
    clipboardLabel: string | null;
    isPinned?: boolean;
    canPin?: boolean;
    onClose: () => void;
    onOpen?: () => void;
    /** "Open with" a specific renderer (undefined = the primary/default);
     *  offered on file targets when more than one renderer applies. */
    onOpenWith?: (rendererId: string | undefined) => void;
    /** The cell the browsed files belong to — enables the "Compare with…"
     *  submenu on file targets (undefined = no compare, e.g. non-cell
     *  browsers). */
    compareCellId?: string;
    onNewFile: (basePath: string) => void;
    onNewFolder: (basePath: string) => void;
    onRename?: () => void;
    onDelete?: () => void;
    onCut?: () => void;
    onCopy?: () => void;
    /** Paste the internal clipboard, else the OS clipboard, into the target
     *  folder. Offered on every writable folder: the OS clipboard cannot be
     *  read without asking, so the menu cannot know whether it is empty. */
    onPaste?: () => void;
    onDuplicate?: () => void;
    onTogglePin?: () => void;
    /** The tree's open/close-every-folder pair, on the background's menu
     *  (read-only included: it changes nothing in the cell). */
    onExpandAll?: () => void;
    onCollapseAll?: () => void;
}

interface Item { label: string; icon: React.ComponentType<{ className?: string }>; action: () => void; danger?: boolean; }

export function FileContextMenu({
    x, y, target, readOnly, locked = false, clipboardLabel, isPinned, canPin,
    onClose, onOpen, onOpenWith, compareCellId, onNewFile, onNewFolder, onRename, onDelete,
    onCut, onCopy, onPaste, onDuplicate, onTogglePin, onExpandAll, onCollapseAll,
}: FileContextMenuProps) {
    const menuRef = useRef<HTMLDivElement>(null);
    // Mounted means open. Historically this menu painted BEHIND pooled
    // renderers (z 100 vs the overlay's 110); with native guest views the
    // failure would be worse — the menu invisible under the view — so
    // guests freeze to their placeholders while any context menu is up.
    useGuestOccluder('all');

    const pos = useViewportClampedPosition(menuRef, x, y);
    useDismissOnOutside(menuRef, onClose);

    const basePath = target.kind === 'empty' ? target.basePath : target.node.path;
    // Only a run writes under `_runs/`: nothing is created or pasted there.
    const canWriteHere = !readOnly && !isRunPath(basePath);
    // A locked node is copied, never cut, renamed, deleted or pinned.
    const canMutate = !readOnly && !locked;
    const pasteLabel = clipboardLabel ? `Paste ${clipboardLabel}` : 'Paste here';
    const items: Item[] = [];

    if (target.kind === 'file') {
        if (onOpen) items.push({ label: 'Open', icon: FolderOpen, action: onOpen });
        if (onCopy) items.push({ label: 'Copy', icon: Copy, action: onCopy });
        if (canMutate && onCut) items.push({ label: 'Cut', icon: Scissors, action: onCut });
        if (!readOnly && onDuplicate) items.push({ label: 'Duplicate', icon: CopyPlus, action: onDuplicate });
        if (canMutate && canPin && onTogglePin) items.push({ label: isPinned ? 'Unpin' : 'Pin as preview', icon: isPinned ? PinOff : Pin, action: onTogglePin });
        if (canMutate && onRename) items.push({ label: 'Rename', icon: Pen, action: onRename });
        if (canMutate && onDelete) items.push({ label: 'Delete', icon: Trash2, action: onDelete, danger: true });
    } else if (target.kind === 'folder') {
        if (onOpen) items.push({ label: 'Open', icon: FolderOpen, action: onOpen });
        if (canWriteHere) items.push({ label: 'New file', icon: FilePlus, action: () => onNewFile(basePath) });
        if (canWriteHere) items.push({ label: 'New folder', icon: FolderPlus, action: () => onNewFolder(basePath) });
        if (onCopy) items.push({ label: 'Copy', icon: Copy, action: onCopy });
        if (canMutate && onCut) items.push({ label: 'Cut', icon: Scissors, action: onCut });
        if (canWriteHere && onPaste) items.push({ label: pasteLabel, icon: ClipboardPaste, action: onPaste });
        if (canMutate && onRename) items.push({ label: 'Rename', icon: Pen, action: onRename });
        if (canMutate && onDelete) items.push({ label: 'Delete', icon: Trash2, action: onDelete, danger: true });
    } else {
        if (canWriteHere) items.push({ label: 'New file', icon: FilePlus, action: () => onNewFile(basePath) });
        if (canWriteHere) items.push({ label: 'New folder', icon: FolderPlus, action: () => onNewFolder(basePath) });
        if (canWriteHere && onPaste) items.push({ label: pasteLabel, icon: ClipboardPaste, action: onPaste });
        if (onExpandAll) items.push({ label: 'Expand all folders', icon: ChevronsUpDown, action: onExpandAll });
        if (onCollapseAll) items.push({ label: 'Collapse all folders', icon: ChevronsDownUp, action: onCollapseAll });
    }

    // "Open with" sits right under Open, file targets only, and only when the
    // filetype actually has more than one applicable renderer.
    const openWithNode = target.kind === 'file' && target.node.fileData && onOpenWith
        && hasOpenWithChoices(target.node.fileData.name)
        ? (
            <OpenWithSubmenu
                filename={target.node.fileData.name}
                onPick={onOpenWith}
                onClose={onClose}
            />
        )
        : null;

    // "Compare with…" sits beside "Open with", file targets only. The submenu
    // renders null itself when the channel offers no compare target.
    const compareNode = target.kind === 'file' && target.node.fileData && compareCellId
        ? (
            <CompareWithSubmenu
                cellId={compareCellId}
                fileName={target.node.fileData.name}
                onClose={onClose}
            />
        )
        : null;

    if (items.length === 0 && !openWithNode && !compareNode) return null;

    return createPortal(
        <div
            ref={menuRef}
            className={`fixed z-[460] min-w-[11.25rem] ${menuSurface}`}
            // no-drag: keeps the popup interactive when it opens inside the
            // desktop title-bar drag band (see FullscreenContentOverlay).
            style={{ WebkitAppRegion: 'no-drag', left: pos.x, top: pos.y } as React.CSSProperties}
        >
            {items.map((item, idx) => (
                <React.Fragment key={idx}>
                    <button
                        onClick={() => { item.action(); onClose(); }}
                        className={item.danger ? menuItemDanger : menuItem}
                    >
                        <item.icon className="w-3.5 h-3.5" />
                        {item.label}
                    </button>
                    {idx === 0 && openWithNode}
                    {idx === 0 && compareNode}
                </React.Fragment>
            ))}
            {items.length === 0 && openWithNode}
            {items.length === 0 && compareNode}
        </div>,
        document.body,
    );
}
