// "Compare with…" — one context-menu row that flies out the diffable targets
// for a file: (a) same-named files in other cells/stacks of the channel, and
// (b) an "Other file in this cell" second-level flyout of the cell's other
// text files. Choosing a target opens the cell's temporary compare tab
// (lib/cellLayout openCompareTab, rendered by cellEditor/CompareTabBody) in
// the right-clicked file's cell, replacing the compare tab already there.
// Shares OpenWithSubmenu's shell conventions: the host menus are hand-rolled
// fixed-position portals, so the flyouts are plain absolute positioning
// inside the row — they must move with the menu and die with it.

import React, { useMemo, useState } from 'react';
import { GitCompareArrows, ChevronRight, FileText } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    CompareSide, compareCellLabel, isComparableTextFile, otherComparableFiles, sameNameCandidates,
} from '@/lib/compare';
import { openCompareTab } from '@/lib/cellLayout';
import { cn } from '@/lib/utils';
import { menuHead, menuItem, menuSep, menuSurface } from '@/components/ui/menuStyles';

interface CompareWithSubmenuProps {
    /** The cell the right-clicked file lives in. */
    cellId: string;
    /** Full (path-style) file name within the cell. */
    fileName: string;
    /** Close the host menu after a pick. */
    onClose: () => void;
}

/** Approximate flyout width used only to decide which side it opens on
 *  (mirrors OpenWithSubmenu). */
const FLYOUT_WIDTH = 240;

export function CompareWithSubmenu({ cellId, fileName, onClose }: CompareWithSubmenuProps) {
    const [open, setOpen] = useState(false);
    const [othersOpen, setOthersOpen] = useState(false);
    // Which side the flyouts open on, decided from the row's live position at
    // hover time (the host menu is viewport-clamped).
    const [flip, setFlip] = useState(false);

    // Snapshot, not subscription: the menu lives for one click.
    const model = useMemo(() => {
        const store = useCollimateStore.getState();
        const loc = store.locateCell(cellId);
        if (!loc) return null;
        const file = loc.cell.files?.find(f => f.name === fileName);
        if (!file || !isComparableTextFile(file)) return null;
        const channel = store.state?.channels.find(c => c.id === loc.channel);
        if (!channel) return null;
        const stack = [...channel.main_stacks, ...channel.nav_stacks]
            .find(s => s.id === loc.stackId);
        const currentLabel = compareCellLabel(stack?.name, loc.cell.name);
        return {
            channelId: channel.id,
            currentLabel,
            sameName: sameNameCandidates(channel, cellId, fileName),
            others: otherComparableFiles(loc.cell, fileName),
        };
    }, [cellId, fileName]);

    if (!model || (model.sameName.length === 0 && model.others.length === 0)) return null;

    const current: CompareSide = {
        channel: model.channelId, cellId, fileName, label: model.currentLabel,
    };
    const pick = (base: CompareSide) => {
        useCollimateStore.getState().updateCellLayout(cellId, l => openCompareTab(l, { base, current }));
        onClose();
    };

    return (
        <div
            className="relative"
            onMouseEnter={(e) => {
                setFlip(e.currentTarget.getBoundingClientRect().right + FLYOUT_WIDTH > window.innerWidth);
                setOpen(true);
            }}
            onMouseLeave={() => { setOpen(false); setOthersOpen(false); }}
        >
            <button
                onClick={() => setOpen(o => !o)}
                aria-haspopup="menu"
                aria-expanded={open}
                className={menuItem}
            >
                <GitCompareArrows className="w-3.5 h-3.5" />
                Compare with
                <ChevronRight className="w-3 h-3 ml-auto" />
            </button>
            {open && (
                <div
                    role="menu"
                    className={cn(
                        'absolute top-0 z-10 min-w-[13.125rem] max-w-[17.5rem]', menuSurface,
                        flip ? 'right-full -mr-0.5' : 'left-full -ml-0.5',
                    )}
                >
                    {model.sameName.length > 0 && (
                        <>
                            <div className={cn(menuHead, 'select-none')}>
                                Same file in…
                            </div>
                            {model.sameName.map((c) => (
                                <button
                                    key={c.cellId}
                                    role="menuitem"
                                    onClick={() => pick({ channel: model.channelId, cellId: c.cellId, fileName: c.fileName, label: c.label })}
                                    className={menuItem}
                                    title={`${c.label} · ${c.fileName}`}
                                >
                                    <GitCompareArrows className="w-3.5 h-3.5 invisible" />
                                    <span className="truncate">{c.label}</span>
                                </button>
                            ))}
                        </>
                    )}
                    {model.others.length > 0 && (
                        <>
                            {model.sameName.length > 0 && <div className={menuSep} />}
                            <div
                                className="relative"
                                onMouseEnter={() => setOthersOpen(true)}
                                onMouseLeave={() => setOthersOpen(false)}
                            >
                                <button
                                    onClick={() => setOthersOpen(o => !o)}
                                    aria-haspopup="menu"
                                    aria-expanded={othersOpen}
                                    className={menuItem}
                                >
                                    <FileText className="w-3.5 h-3.5" />
                                    Other file in this cell
                                    <ChevronRight className="w-3 h-3 ml-auto" />
                                </button>
                                {othersOpen && (
                                    <div
                                        role="menu"
                                        className={cn(
                                            'absolute top-0 z-10 min-w-[11.25rem] max-w-[16.25rem] max-h-[50vh] overflow-y-auto', menuSurface,
                                            flip ? 'right-full -mr-0.5' : 'left-full -ml-0.5',
                                        )}
                                    >
                                        {model.others.map((f) => (
                                            <button
                                                key={f.name}
                                                role="menuitem"
                                                onClick={() => pick({ channel: model.channelId, cellId, fileName: f.name, label: model.currentLabel })}
                                                className={menuItem}
                                                title={f.name}
                                            >
                                                <FileText className="w-3.5 h-3.5" />
                                                <span className="truncate">{f.name}</span>
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </>
                    )}
                </div>
            )}
        </div>
    );
}
