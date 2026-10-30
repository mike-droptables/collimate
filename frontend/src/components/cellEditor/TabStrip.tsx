// The 30px tab strip of one content-pane leaf (row C13, the mock's `.tabs`;
// DESIGN §3.3, rules R17/R22). Every item sits on the strip's top 18px line,
// the stack head's grid; the open tab carries a short rule under its name,
// inset 12px from its edges: brass in the selected cell, teal elsewhere.
//
// A tab reads, in fixed slots: 12px padding, the name (the one part that
// shrinks: it ellipsises first), 6px, the close glyph's 18px slot, 12px
// padding. Close never sits over the text and never moves it when it
// appears (owner polish 2026-10-07: wider tabs, over the mock's 10px
// padding). Saving shows nothing on a tab (owner, 2026-10-13); a save that
// fails is reported in the file's editor.
//
// The drag protocol is load-bearing and easy to undo by accident: only a
// marker MIME rides on the dataTransfer, because `dataTransfer.getData()` is
// unreadable during `dragover`. The payload (cell/leaf/tab ids) lives in the
// store's `tabDrag` slice, which is how the drop targets know the insertion
// point before the drop lands. Same convention as useFileTransfer's file
// drags.

import React, { useRef, useState } from 'react';
import { Pin, Plus, X, Maximize2, Minimize2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useShortcutDisplay } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { activateTab, closeTab, cycleFilesPaneSide, isTabPinned, moveTab, openEmptyTab, togglePinnedTabCompact } from '@/lib/cellLayout';
import { useFilesDockDrag } from '@/hooks/useFilesDockDrag';
import { FILES_SIDE_ICON, filesDockHint } from '@/lib/filesDock';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { lockOwner } from '@/lib/runningCell';
import { runDisplayName } from '@/lib/runLabels';
import type { FilesPaneSide, CellLayout, CellTab, ContentLeaf } from '@/stores/collimate/types';
import type { RunRecord } from '@/lib/types';
import { TabContextMenu } from './TabContextMenu';
import { RunsPaneToggle } from './RunsPaneToggle';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { chip } from '@/components/ui/formStyles';
import { compareSideTitle } from '@/lib/compare';

/** Marker MIME for a content-tab drag (payload lives in store.tabDrag). Only
 *  this file writes it and nothing reads it back — the drop targets identify a
 *  tab drag by the store slice, because getData() is unreadable during
 *  dragover. It rides along so the OS/other drop targets can tell this drag
 *  apart from a file drag. Module-local on purpose. */
const COLLIMATE_TAB_MIME = 'application/x-collimate-tab';

const basename = (path: string): string => path.split('/').filter(Boolean).pop() ?? path;

/** `frame.md` → `frame` + `.md` (a dotfile is all extension). */
function splitName(name: string): { stem: string; ext: string } {
    const dot = name.lastIndexOf('.');
    return dot < 0 ? { stem: name, ext: '' } : { stem: name.slice(0, dot), ext: name.slice(dot) };
}

/** A tool beside the tabs (the mock's `.tabs .tool`, `.files-btn`): an
 *  18px line, centred on the tabs' text. */
const TOOL = 'flex-none h-[1.125rem] self-end mb-1 inline-flex items-center rounded-xs text-ink-3 transition-colors hover:text-ink hover:bg-sheet-2';
/** A tab: a 26px rounded-top frame standing on the strip's hairline, like
 *  a tab on a folder. The open one is the sheet in ink, edged in line-2
 *  with its top edge in the cell's accent (brass in the selected cell,
 *  teal elsewhere), and its sheet covers the hairline under it, so it
 *  joins the body it opens; the rest are frameless ink-2 until hovered.
 *  No rule under the tabs and no line between them: the frame says which
 *  is open. */
const TAB = 'group relative flex-none flex items-stretch h-[1.625rem] rounded-t-md border border-b-0 border-t-2 transition-colors';
const TAB_OPEN = 'bg-sheet text-ink border-line-2';
const TAB_CLOSED = 'border-transparent text-ink-2 hover:text-ink hover:bg-sheet-2';
/** A chip on the tab's line: the chip style at the line's height. */
const TAB_CHIP = cn(chip, 'h-4 px-1 leading-none');
/** A tab's widest extent; past it the name ellipsises. */
const TAB_MAX_W = 'max-w-[15rem]';

/** A tab is text. A file tab's one icon is the pin on the pinned file —
 *  that mark is the point of pinning, so no file-type glyph competes with
 *  it. History, compare and empty tabs carry none: the label and the
 *  tooltip say what a tab is. The compare tab,
 *  the cell's one temporary tab, reads in italic. */
function tabPresentation(tab: CellTab, pinned: boolean, owner: RunRecord | null): {
    /** The pinned file tab: the pin leads it (and folds it, see `compact`). */
    pin?: boolean;
    label: string;
    title: string;
    /** "<runner name> <short id>" on a live run's file: the tab reads
     *  "<basename> · <run>" (the run shown as a chip). */
    run?: string;
    /** The temporary tab (a compare): its name reads in italic. */
    temporary?: boolean;
} {
    switch (tab.kind) {
        case 'file': {
            const run = owner ? runDisplayName(owner) : undefined;
            // Surface an "Open with" override in the tooltip so a
            // non-primary renderer isn't a silent mystery.
            const path = tab.rendererId ? `${tab.path} · ${tab.rendererId}` : tab.path;
            return {
                pin: pinned,
                label: basename(tab.path),
                title: run ? `${path} — ${run}` : path,
                run,
            };
        }
        case 'empty':
            return { label: 'New tab', title: 'Empty tab' };
        case 'history':
            return {
                label: basename(tab.path),
                title: `History · ${tab.category} · ${tab.path}`,
            };
        case 'compare':
            return {
                label: basename(tab.current.fileName),
                title: `Compare · ${compareSideTitle(tab.base)} vs ${compareSideTitle(tab.current)}`,
                temporary: true,
            };
    }
}

export interface TabStripProps {
    cellId: string;
    /** cell.pinned when it resolves to a real file (derives pinned-ness). */
    pinnedPath: string | null;
    /** The pinned tab shows only its pin (CellLayout.pinnedTabCompact). */
    pinnedTabCompact?: boolean;
    leaf: ContentLeaf;
    onLayoutChange: (fn: (layout: CellLayout) => CellLayout) => void;
    /** saveNow drain — structural ops remount moved subtrees. */
    onBeforeStructuralChange: () => void | Promise<void>;
    onToggleMaximized: () => void;
    /** True while the cell's content region is expanded to the fullscreen
     *  overlay — the expand button (Maximize2) flips to its inverse,
     *  Minimize2, which brings the region back to the canvas. */
    maximized?: boolean;
    onCloseFileTab: (path: string) => void;
    onTogglePin?: (fileName: string) => void;
    /** Present while the Files pane is hidden, on the cell's first leaf: the
     *  strip leads with the pane's toggle (the mock's `.files-btn`), its icon
     *  naming the side the pane will dock on when shown. */
    showFiles?: { side: FilesPaneSide; onShow: () => void };
    /** The cell sits on the Capabilities stack: no run ever holds it, so the
     *  full-window view shows no status caret. */
    capabilitiesCell?: boolean;
}

export const TabStrip: React.FC<TabStripProps> = ({
    cellId, pinnedPath, pinnedTabCompact = false, leaf, onLayoutChange,
    onBeforeStructuralChange,
    onToggleMaximized, maximized = false, onCloseFileTab, onTogglePin, showFiles,
    capabilitiesCell = false,
}) => {
    const setTabDrag = useCollimateStore(s => s.setTabDrag);
    // The Show files toggle docks by drag too: a release over an edge of
    // the cell docks the pane there and shows it.
    const dockDrag = useFilesDockDrag(cellId, onLayoutChange);
    // Brass marks the selection only: the open tab's rule and the pin.
    const selected = useCellSelected();
    // The live runs, for the run chip on a run's file tabs. A UI-file tab
    // is an ordinary tab otherwise: closable, draggable, splittable.
    const runs = useCollimateStore(s => s.runs);
    const stoppingRunIds = useCollimateStore(s => s.stoppingRunIds);
    const ownerOf = (tab: CellTab): RunRecord | null => (tab.kind === 'file'
        ? lockOwner({ runs, stoppingRunIds }, cellId, tab.path)
        : null);
    const tabsRef = useRef<HTMLDivElement>(null);
    // Where a dropped tab would land: list index + the x offset (in the
    // strip's scrolled content coordinates) of the insertion bar.
    const [insertion, setInsertion] = useState<{ index: number; x: number } | null>(null);
    // Mirrored in a ref: `drop` must read the index computed by the LAST
    // `dragover`, and React may not have flushed that state update yet.
    const insertionRef = useRef<{ index: number; x: number } | null>(null);
    const putInsertion = (next: { index: number; x: number } | null) => {
        const cur = insertionRef.current;
        if (cur === next || (cur && next && cur.index === next.index && cur.x === next.x)) return;
        insertionRef.current = next;
        setInsertion(next);
    };
    const [menu, setMenu] = useState<{ x: number; y: number; tab: CellTab } | null>(null);
    // Live keybinding display so the chrome advertises the actual combo (and
    // picks up user remaps) instead of hardcoding "Esc".
    const escDisplay = useShortcutDisplay(SHORTCUT_IDS.EDITOR_CLOSE);

    const activeTab = leaf.tabs.find(t => t.id === leaf.activeTabId) ?? null;

    const handleClose = async (tab: CellTab) => {
        try {await onBeforeStructuralChange();} catch {return;}
        // A file tab closes through CellEditor's path-keyed
        // handleCloseFileTab; everything else is pure layout surgery. (The
        // version checkpoint and the mainFocusMode reset ride on CellEditor
        // effects: the open-path diff and R12, keyed on the active file.)
        if (tab.kind === 'file') onCloseFileTab(tab.path);
        else onLayoutChange(l => closeTab(l, tab.id, pinnedPath));
    };

    const dragForThisCell = (): boolean =>
        useCollimateStore.getState().tabDrag?.cellId === cellId;

    const handleStripDragOver = (e: React.DragEvent) => {
        if (!dragForThisCell()) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = 'move';
        const cont = tabsRef.current;
        if (!cont) return;
        const contRect = cont.getBoundingClientRect();
        const nodes = Array.from(cont.querySelectorAll<HTMLElement>('[data-tab-id]'));
        const toContentX = (clientX: number) => clientX - contRect.left + cont.scrollLeft;
        let index = nodes.length;
        let x = 0;
        for (let i = 0; i < nodes.length; i++) {
            const r = nodes[i].getBoundingClientRect();
            if (e.clientX < r.left + r.width / 2) { index = i; x = toContentX(r.left); break; }
        }
        if (index === nodes.length) {
            const last = nodes[nodes.length - 1];
            x = last ? toContentX(last.getBoundingClientRect().right) : 0;
        }
        putInsertion({ index, x });
    };

    const handleStripDrop = async (e: React.DragEvent) => {
        const drag = useCollimateStore.getState().tabDrag;
        const index = insertionRef.current?.index ?? leaf.tabs.length;
        putInsertion(null);
        // Tab drags are cell-scoped: a tab from another cell is not ours to move.
        if (!drag || drag.cellId !== cellId) return;
        e.preventDefault();
        e.stopPropagation();
        try {await onBeforeStructuralChange();} catch {return;}
        onLayoutChange(l => moveTab(l, drag.tabId, leaf.id, index));
        setTabDrag(null);
    };

    return (
        <div
            // The hairline is drawn behind the tabs (a negative-z pseudo in the
            // strip's own stacking context), not as a border: the open tab's
            // sheet sits on it and breaks it, as the strip's drop-in note says.
            className="@container relative isolate flex-none flex items-end h-[1.875rem] pr-1 bg-sheet text-[0.78125rem] after:pointer-events-none after:absolute after:inset-x-0 after:bottom-0 after:-z-10 after:h-px after:bg-line"
        >
            {showFiles && (
                <button
                    type="button"
                    aria-label="Show files"
                    aria-pressed={false}
                    title={`Show files · ${filesDockHint(showFiles.side)}`}
                    onClick={showFiles.onShow}
                    // A right-click docks on the other side without
                    // opening the pane, as the pane's Hide icon does: the
                    // icon turns, the next click opens it there.
                    onContextMenu={(e) => { e.preventDefault(); onLayoutChange(cycleFilesPaneSide); }}
                    {...dockDrag}
                    // The Files pane's toggle, always off here (it shows only
                    // while the pane is hidden); the pressed look is on the
                    // pane rail's Hide files (FilesPane).
                    className={cn(TOOL, 'ml-1.5 mr-1 px-[0.3125rem]')}
                >
                    {React.createElement(FILES_SIDE_ICON[showFiles.side], { className: 'w-3.5 h-3.5' })}
                </button>
            )}
            <div
                ref={tabsRef}
                role="tablist"
                className="relative flex-1 min-w-0 h-full flex items-end gap-0.5 px-0.5 overflow-x-auto overflow-y-hidden"
                // Firefox's scrollbar would eat 12 of the strip's 30px.
                style={{ scrollbarWidth: 'none' }}
                onDragOver={handleStripDragOver}
                onDragLeave={(e) => {
                    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
                    putInsertion(null);
                }}
                onDrop={handleStripDrop}
            >
                {leaf.tabs.map(tab => {
                    const active = tab.id === leaf.activeTabId;
                    const pinned = isTabPinned(tab, pinnedPath);
                    const { pin, label, title, run, temporary } = tabPresentation(tab, pinned, ownerOf(tab));
                    // File, history and compare tabs show the extension in mono.
                    const { stem, ext } = tab.kind === 'file' || tab.kind === 'history' || tab.kind === 'compare'
                        ? splitName(label) : { stem: label, ext: '' };
                    // The pinned tab folded to its pin alone. The pin is the
                    // control: on the active pinned tab it toggles the fold,
                    // on an inactive one it only activates the tab.
                    const compact = pinned && pinnedTabCompact;
                    const onPinClick = (e: React.MouseEvent) => {
                        e.stopPropagation();
                        if (active) onLayoutChange(togglePinnedTabCompact);
                        else onLayoutChange(l => activateTab(l, tab.id));
                    };
                    return (
                        <div
                            key={tab.id}
                            role="presentation"
                            data-tab-id={tab.id}
                            draggable
                            onDragStart={(e) => {
                                setTabDrag({ cellId, leafId: leaf.id, tabId: tab.id });
                                e.dataTransfer.setData(COLLIMATE_TAB_MIME, '1');
                                e.dataTransfer.effectAllowed = 'move';
                            }}
                            onDragEnd={() => { setTabDrag(null); putInsertion(null); }}
                            // A middle click closes the tab as its close glyph
                            // does, through the same handler; a pinned tab,
                            // which shows no glyph, stays. The press itself is
                            // swallowed so it neither starts the autoscroll
                            // nor pastes the primary selection (Linux).
                            onMouseDown={(e) => { if (e.button === 1) e.preventDefault(); }}
                            onAuxClick={(e) => {
                                if (e.button !== 1) return;
                                e.preventDefault();
                                e.stopPropagation();
                                if (!pinned) handleClose(tab);
                            }}
                            onContextMenu={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                setMenu({ x: e.clientX, y: e.clientY, tab });
                            }}
                            className={cn(TAB, TAB_MAX_W, active ? [TAB_OPEN, selected ? 'border-t-brass' : 'border-t-teal'] : TAB_CLOSED)}
                        >
                            <button
                                role="tab"
                                aria-selected={active}
                                aria-label={compact ? label : undefined}
                                title={compact ? `${title} — click the pin to show the name` : title}
                                onClick={() => onLayoutChange(l => activateTab(l, tab.id))}
                                onDoubleClick={() => onToggleMaximized()}
                                // A closable tab's right padding sits after
                                // its close slot, outside this button.
                                className={cn('h-full min-w-0 pl-3 inline-flex items-center gap-1.5 select-none', pinned && 'pr-3', temporary && 'italic')}
                            >
                                {pin && (
                                    <span
                                        data-testid="tab-pin"
                                        title={active
                                            ? (compact ? 'Show the name' : 'Show only the pin')
                                            : undefined}
                                        onClick={onPinClick}
                                        className={cn('flex-none inline-flex', selected ? 'text-brass' : 'text-teal')}
                                    >
                                        <Pin className="w-3.5 h-3.5 flex-none" aria-hidden />
                                    </span>
                                )}
                                {!compact && stem && <span className="min-w-0 truncate">{stem}</span>}
                                {!compact && ext && (
                                    <span className={cn('flex-none font-mono text-[0.6875rem]', active ? 'text-ink-2' : 'text-ink-3')}>{ext}</span>
                                )}
                                {!compact && run && (
                                    <span
                                        data-testid="tab-run-chip"
                                        className={cn(TAB_CHIP, 'block max-w-[5.625rem] truncate font-mono text-[0.625rem] leading-[0.875rem]')}
                                    >
                                        <span className="sr-only"> · </span>{run}
                                    </span>
                                )}
                            </button>
                            {/* Pinned tabs render no ✕ at all — closeTab refuses
                                them anyway, so an inert button would just lie.
                                Close has its own 18px slot after the name,
                                always held, and shows on hover or focus: it
                                never covers the name, and showing it never
                                moves the tab. Hidden is transparent and the
                                fade is a colour fade: an opacity fade runs
                                on the compositor and turns the cell's text
                                grayscale while it runs. */}
                            {!pinned && (
                                <button
                                    onClick={(e) => { e.stopPropagation(); handleClose(tab); }}
                                    title={tab.kind === 'file' || tab.kind === 'compare' ? `Close — double-tap ${escDisplay || 'Esc'}` : 'Close tab'}
                                    aria-label={`Close ${label}`}
                                    className="flex-none w-[1.125rem] h-full ml-1.5 mr-3 inline-flex items-center justify-center rounded-xs text-ink-3 hover:text-ink hover:bg-sheet-3 not-focus:not-group-hover:text-transparent pointer-events-none group-hover:pointer-events-auto focus:pointer-events-auto transition-colors"
                                >
                                    <X className="w-3 h-3" />
                                </button>
                            )}
                        </div>
                    );
                })}
                {/* New tab — rides the tab row, directly right of the last
                    tab (not parked at the strip's far edge). */}
                <button
                    onClick={() => onLayoutChange(l => openEmptyTab(l, leaf.id))}
                    title="New tab"
                    aria-label="New tab"
                    className={cn(TOOL, 'px-2')}
                >
                    <Plus className="w-3.5 h-3.5" />
                </button>
                {insertion && (
                    <div
                        className="absolute top-0 bottom-0 w-[2px] bg-teal pointer-events-none z-10"
                        style={{ left: Math.max(0, insertion.x - 1) }}
                        aria-hidden
                    />
                )}
            </div>

            {/* The full-window view hides the cell header, so its status
                caret (RunsPaneToggle) sits here, left of the collapse
                button, shown whether or not anything runs — on any cell but
                a Capabilities cell, which has no status pane. */}
            {maximized && !capabilitiesCell && (
                <RunsPaneToggle cellId={cellId} className={cn(TOOL, 'px-[0.3125rem] mr-0.5')} data-testid="runs-pane-toggle-strip" />
            )}
            {activeTab && (
                <button
                    onClick={() => onToggleMaximized()}
                    title={maximized ? 'Back to the canvas' : 'Expand the cell view to the full window'}
                    aria-label={maximized ? 'Back to the canvas' : 'Expand the cell view to the full window'}
                    className={cn(TOOL, 'px-2')}
                >
                    {maximized ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                </button>
            )}

            {menu && (
                <TabContextMenu
                    x={menu.x}
                    y={menu.y}
                    leafId={leaf.id}
                    cellId={cellId}
                    tab={menu.tab}
                    isPinned={isTabPinned(menu.tab, pinnedPath)}
                    pinnedPath={pinnedPath}
                    // A run's file is never the cell's preview: it vanishes
                    // with the run.
                    canPin={!!onTogglePin && !ownerOf(menu.tab)}
                    onClose={() => setMenu(null)}
                    onLayoutChange={onLayoutChange}
                    onBeforeStructuralChange={onBeforeStructuralChange}
                    onCloseFileTab={onCloseFileTab}
                    onTogglePin={onTogglePin}
                />
            )}
        </div>
    );
};
