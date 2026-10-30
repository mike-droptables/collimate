import { useViewportClampedPosition, useDismissOnOutside } from '@/hooks/usePopoverPosition';
// Right-click menu for a content-pane tab (DESIGN §3.4, rules R22/R23).
// Items stay local; viewport clamping and outside/iframe dismissal share
// hooks with the other body-portal popovers. Guest occlusion stays mounted
// for the menu's lifetime so native views cannot paint over it.

import React, { useRef } from 'react';
import { createPortal } from 'react-dom';
import { X, CopyX, ArrowRightToLine, Pin, PinOff, Columns2, Rows2 } from 'lucide-react';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import {
    closeOtherTabs, closeTab, closeTabsToRight, setTabRenderer, splitWithTab,
} from '@/lib/cellLayout';
import type { CellLayout, CellTab } from '@/stores/collimate/types';
import { OpenWithSubmenu, hasOpenWithChoices } from '@/components/OpenWithSubmenu';
import { CompareWithSubmenu } from '@/components/CompareWithSubmenu';
import { resolveRendererWithOverride } from '@/hooks/useRendererId';
import { menuItem, menuSep, menuSurface } from '@/components/ui/menuStyles';

export interface TabContextMenuProps {
    x: number;
    y: number;
    /** The leaf the tab currently lives in — the split ops need it. */
    leafId: string;
    /** The owning cell — enables "Compare with…" on file tabs. */
    cellId?: string;
    tab: CellTab;
    /** Derived (never stored): this is the pinned file's tab. */
    isPinned: boolean;
    /** cell.pinned when it resolves to a real file — the closeTab ops need it
     *  to refuse pinned tabs. */
    pinnedPath: string | null;
    /** False when the cell is read-only / its files are locked. */
    canPin: boolean;
    onClose: () => void;
    onLayoutChange: (fn: (layout: CellLayout) => CellLayout) => void;
    /** saveNow drain — every structural op remounts moved subtrees. */
    onBeforeStructuralChange: () => void | Promise<void>;
    /** Closing a FILE tab routes through CellEditor (focus mode + checkpoint). */
    onCloseFileTab: (path: string) => void;
    onTogglePin?: (fileName: string) => void;
}

type Item =
    | { kind: 'divider' }
    | {
        kind: 'item';
        label: string;
        icon: React.ComponentType<{ className?: string }>;
        action: () => void;
        disabled?: boolean;
    };

export function TabContextMenu({
    x, y, leafId, cellId, tab, isPinned, pinnedPath, canPin,
    onClose, onLayoutChange, onBeforeStructuralChange, onCloseFileTab, onTogglePin,
}: TabContextMenuProps) {
    const menuRef = useRef<HTMLDivElement>(null);
    useGuestOccluder('all');

    const pos = useViewportClampedPosition(menuRef, x, y);
    useDismissOnOutside(menuRef, onClose);

    const structural = async (fn: (layout: CellLayout) => CellLayout) => {
        try {await onBeforeStructuralChange();} catch {return;}
        onLayoutChange(fn);
    };

    const items: Item[] = [
        {
            kind: 'item',
            label: 'Close',
            icon: X,
            disabled: isPinned,
            action: async () => {
                try {await onBeforeStructuralChange();} catch {return;}
                if (tab.kind === 'file') onCloseFileTab(tab.path);
                else onLayoutChange(l => closeTab(l, tab.id, pinnedPath));
            },
        },
        {
            kind: 'item',
            label: 'Close others',
            icon: CopyX,
            action: () => structural(l => closeOtherTabs(l, tab.id, pinnedPath)),
        },
        {
            kind: 'item',
            label: 'Close to the right',
            icon: ArrowRightToLine,
            action: () => structural(l => closeTabsToRight(l, tab.id, pinnedPath)),
        },
    ];

    // Pin is a FILE-tab affordance: it flips cell.pinned, which is what makes
    // a tab pinned in the first place.
    if (tab.kind === 'file' && canPin && onTogglePin) {
        const path = tab.path;
        items.push({ kind: 'divider' });
        items.push({
            kind: 'item',
            label: isPinned ? 'Unpin' : 'Pin as preview',
            icon: isPinned ? PinOff : Pin,
            action: () => onTogglePin(path),
        });
    }

    // "Open with" — switch this open tab to another renderer that supports the
    // filetype. A structural-grade change: the retarget remounts the renderer,
    // so pending edits are flushed first (the pick handler below).
    const openWithNode = tab.kind === 'file' && hasOpenWithChoices(tab.path)
        ? (
            <OpenWithSubmenu
                filename={tab.path}
                currentId={resolveRendererWithOverride(tab.path, tab.rendererId).id}
                onPick={async (rendererId) => {
                    try {await onBeforeStructuralChange();} catch {return;}
                    onLayoutChange(l => setTabRenderer(l, tab.id, rendererId));
                }}
                onClose={onClose}
            />
        )
        : null;

    // "Compare with…" — diff this tab's file against a same-named file in
    // another cell/stack, or another file in this cell. Renders null when
    // there is nothing to compare against.
    const compareNode = tab.kind === 'file' && cellId
        ? (
            <CompareWithSubmenu
                cellId={cellId}
                fileName={tab.path}
                onClose={onClose}
            />
        )
        : null;

    items.push({ kind: 'divider' });
    items.push({
        kind: 'item',
        label: 'Split right',
        icon: Columns2,
        action: () => structural(l => splitWithTab(l, leafId, tab.id, 'right')),
    });
    items.push({
        kind: 'item',
        label: 'Split down',
        icon: Rows2,
        action: () => structural(l => splitWithTab(l, leafId, tab.id, 'bottom')),
    });

    return createPortal(
        <div
            ref={menuRef}
            className={`fixed z-[460] min-w-[11.25rem] ${menuSurface}`}
            // no-drag: keeps the popup interactive when it opens inside the
            // desktop title-bar drag band (see FullscreenContentOverlay).
            style={{ WebkitAppRegion: 'no-drag', left: pos.x, top: pos.y } as React.CSSProperties}
        >
            {openWithNode}
            {compareNode}
            {(openWithNode || compareNode) && <div className={menuSep} />}
            {items.map((item, idx) => (
                item.kind === 'divider' ? (
                    <div key={idx} className={menuSep} />
                ) : (
                    <button
                        key={idx}
                        disabled={item.disabled}
                        onClick={() => { item.action(); onClose(); }}
                        className={menuItem}
                    >
                        <item.icon className="w-3.5 h-3.5" />
                        {item.label}
                    </button>
                )
            ))}
        </div>,
        document.body,
    );
}
