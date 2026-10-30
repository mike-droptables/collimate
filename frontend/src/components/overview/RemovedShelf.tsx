import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { motion, useReducedMotion, useTransform } from 'motion/react';
import { Search, SlidersHorizontal, Tag, X } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import type { RemovedCell, RemovedSort, SortDir } from '@/lib/api/cells';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { hasTag, mergeVocabulary, tagMatches } from '@/lib/tags';
import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    JOURNAL_PANE_WIDTH, OVERVIEW_CARD_HEIGHT, connectedCells, connectedOn, shelfEntries, shelfFollowHint, subjectCellId,
} from '@/stores/collimate/overviewSlice';
import { cn } from '@/lib/utils';
import { commandPaneInsetOf, paneInsetTransition } from '@/lib/commandPane';
import { useRefusalShake, useShelfDrag, type ShelfDrag } from '@/hooks/useShelfDrag';
import { RemovedCard } from './RemovedCard';
import { RemoveZonesEditor } from './RemoveZonesEditor';
import { SHELF_CARD_MIN, SHELF_GAP, SHELF_PAD, shelfColumns } from './shelfColumns';
import { ZoneStrip } from './ZoneStrip';

// The Removed shelf (the mock's `.rm-shelf` and `renderShelf`): above the
// Removed bar, as tall as the bar's animated `--ov-shelf`, from the left
// edge to the journal pane. A control row (Sort by, Order, the search,
// the tag filter chips (teal: a filter that is on), the tag toggle (every board card shows its tags in
// its body), the remove zones' config button, the count) over a
// grid of the removed cells as ghost cards that fit the shelf's height. The
// cards come from the store: the shelf route's list, sorted on the client
// in the server's order, narrowed by the server's matches for the search
// (`q`, which matches tags too), by the tag filter chips and by Only
// connected. While typing, the search lists the shelf's tags that match;
// Enter or a click makes the match a chip (chips AND together) and clears
// the text, Backspace on an empty search drops the last chip.
//
// Cards cross between the shelf and the board: a shelf card dragged onto the
// board is restored there (useShelfDrag; the grid reflows without it while
// it is lifted and a dimmed ghost follows the pointer), and a board card
// dragged over the open shelf splits it into the remove zones (ZoneStrip, the
// bar's split) and lights the zone under it (useStackDrag removes it with
// that zone's action on the drop, or the shelf shakes and says why it
// cannot).
//
// Under an open Commands pane the shelf's ground and its control row's
// hairline run on under the pane, so the pane reads as laid over the page;
// the control row, the grid and the drop hint keep clear of it, sliding as
// it opens, closes or docks across.

/** Two clicks on one card within this long are a double click. */
const DOUBLE_CLICK_MS = 800;
/** The search lists at most this many matching tags. */
const TAG_SUGGESTIONS_MAX = 8;

const segOn = 'bg-sheet-2 text-ink';
const segOff = 'text-ink-2 hover:bg-sheet-2 hover:text-ink';

function Segmented<V extends string>({ label, value, options, onChange }: {
    label: string; value: V; options: [V, string][]; onChange: (value: V) => void;
}) {
    return (
        <span role="group" aria-label={label} className="mr-1.5 inline-flex flex-none gap-0.5">
            {options.map(([v, text]) => (
                <button
                    key={v}
                    type="button"
                    aria-pressed={value === v}
                    className={cn('h-6 flex-none rounded-sm px-2 text-[0.78125rem] transition-colors', value === v ? segOn : segOff)}
                    onClick={() => onChange(v)}
                >
                    {text}
                </button>
            ))}
        </span>
    );
}

/** The lifted shelf card, dimmed, under the pointer where it was held. */
function ShelfGhost({ drag, entry, sort }: { drag: ShelfDrag; entry: RemovedCell; sort: RemovedSort }) {
    const lifted = drag.lifted!;
    const x = useTransform(drag.pointerX, v => v - lifted.grabX);
    const y = useTransform(drag.pointerY, v => v - lifted.grabY);
    return createPortal(
        <motion.div
            data-rm-ghost={entry.cell_id}
            aria-hidden
            className="pointer-events-none fixed top-0 left-0 z-[470] opacity-55"
            style={{ x, y, width: lifted.width, ['--rm-ch' as string]: `${lifted.height}px` }}
        >
            <RemovedCard entry={entry} sort={sort} selected={false} journalStyle={false} followHint={false} onClick={() => {}} onRestore={() => {}} />
        </motion.div>,
        document.body,
    );
}

/** The name a cell goes by, wherever the overview knows it from. */
function useCellName(cellId: string | null): string {
    return useCollimateStore(s => {
        if (!cellId) return '';
        const removed = s.overviewData.removed?.find(entry => entry.cell_id === cellId);
        if (removed) return removed.name;
        for (const stack of s.activeChannel?.main_stacks ?? []) {
            const cell = stack.cells.find(c => String(c.id) === cellId);
            if (cell) return cell.name;
        }
        return s.overviewData.lineage?.cells.find(c => c.cell_id === cellId)?.name ?? '';
    });
}

/** The shelf's columns from the overview's width (the shelf's parent,
 *  which no pane narrows), less the journal pane while it is open. */
function useShelfColumns(shelfRef: React.RefObject<HTMLDivElement | null>, journalOpen: boolean): string {
    const [width, setWidth] = useState(0);
    useLayoutEffect(() => {
        const root = shelfRef.current?.parentElement;
        if (!root) return undefined;
        setWidth(root.clientWidth);
        if (typeof ResizeObserver === 'undefined') return undefined;
        const ro = new ResizeObserver(() => setWidth(root.clientWidth));
        ro.observe(root);
        return () => ro.disconnect();
    }, [shelfRef]);
    if (width === 0) return `repeat(auto-fill, minmax(${SHELF_CARD_MIN}px, 1fr))`;
    return shelfColumns(width - (journalOpen ? JOURNAL_PANE_WIDTH : 0));
}

export function RemovedShelf() {
    const shelfOpen = useCollimateStore(s => s.overview.shelfOpen);
    const journalOpen = useCollimateStore(s => s.overview.journalOpen);
    const paneInset = useCollimateStore(s => commandPaneInsetOf(s.commandPane));
    // An open journal docks against the pane's inner edge (JournalPane):
    // the shelf ends at the journal, the pane's width further in.
    const paneRight = journalOpen ? 0 : paneInset.right;
    const reduceMotion = useReducedMotion();
    const sort = useCollimateStore(s => s.overview.sort);
    const dir = useCollimateStore(s => s.overview.dir);
    const query = useCollimateStore(s => s.overview.query);
    const tagFilters = useCollimateStore(s => s.overview.tagFilters);
    const removeZones = useCollimateStore(s => s.overview.removeZones);
    const removedList = useCollimateStore(s => s.overviewData.removed);
    const shelfSelection = useCollimateStore(s => s.overview.shelfSelection);
    const scrollTo = useCollimateStore(s => s.overview.scrollTo);
    const total = useCollimateStore(s => s.overviewData.removed?.length ?? 0);
    const loaded = useCollimateStore(s => s.overviewData.removed !== null);
    const entries = useCollimateStore(useShallow(shelfEntries));
    const narrowed = useCollimateStore(s => connectedCells(s) !== null);
    const subject = useCollimateStore(subjectCellId);
    const subjectName = useCellName(subject);
    const shelfSubject = useCollimateStore(s => {
        const id = subjectCellId(s);
        return id && s.overviewData.removed?.some(entry => entry.cell_id === id) ? id : null;
    });
    const hinted = useCollimateStore(s => s.overview.picked && shelfFollowHint(s, s.overview.picked) ? s.overview.picked : null);
    const {
        setRemovedSort, setRemovedDir, setRemovedQuery, pickShelf, anchorGhost, restoreRemoved, addTagFilter, removeTagFilter,
    } = useCollimateStore(useShallow(s => ({
        setRemovedSort: s.setRemovedSort, setRemovedDir: s.setRemovedDir, setRemovedQuery: s.setRemovedQuery,
        pickShelf: s.pickShelf, anchorGhost: s.anchorGhost, restoreRemoved: s.restoreRemoved,
        addTagFilter: s.addTagFilter, removeTagFilter: s.removeTagFilter,
    })));
    const showTags = useCollimateStore(s => s.overview.showTags);

    // A board card held over the open shelf: the shelf is its drop target.
    const dropHere = useCollimateStore(s => (s.overview.drop?.kind === 'remove' && s.overview.drop.over === 'shelf' ? s.overview.drop : null));

    const shelfRef = useRef<HTMLDivElement>(null);
    const gridRef = useRef<HTMLDivElement>(null);
    const searchRef = useRef<HTMLLabelElement>(null);
    const zonesButtonRef = useRef<HTMLButtonElement>(null);
    const [zonesOpen, setZonesOpen] = useState(false);
    const [searchFocused, setSearchFocused] = useState(false);
    const [activeTag, setActiveTag] = useState(0);
    const [listBox, setListBox] = useState<{ left: number; width: number } | null>(null);
    const suggestionsId = useId();
    const lastClick = useRef<{ t: number; id: string; followed: boolean } | null>(null);
    const [restoring, setRestoring] = useState<string | null>(null);
    const [failure, setFailure] = useState<string | null>(null);
    // The shelf keeps its cards while it slides shut, then lets them go.
    const [mounted, setMounted] = useState(shelfOpen);

    useGuestOccluder('all', shelfOpen);
    // Shelf cards drag as board cards do: not while Only connected keeps a set.
    const drag = useShelfDrag(shelfOpen && !narrowed);
    const refused = useRefusalShake(shelfRef, 'shelf');
    // The cards' width comes from the shelf's whole width, the Commands
    // pane's share included: the pane lies over the shelf and pushes its
    // cards aside (fewer to a row) without resizing them.
    const columns = useShelfColumns(shelfRef, journalOpen);
    const liftedEntry = drag.lifted ? entries.find(entry => entry.cell_id === drag.lifted!.cellId) ?? null : null;
    const shown = liftedEntry ? entries.filter(entry => entry !== liftedEntry) : entries;

    if (shelfOpen && !mounted) setMounted(true);
    useEffect(() => {
        if (shelfOpen) return undefined;
        const timer = setTimeout(() => setMounted(false), 250);
        return () => clearTimeout(timer);
    }, [shelfOpen]);

    // The selected card carries Restore; with none chosen (or the chosen
    // one filtered away) the first card does.
    const selected = entries.some(entry => entry.cell_id === shelfSelection)
        ? shelfSelection
        : entries[0]?.cell_id ?? null;

    // The cards keep the wheel (it scrolls them); over the control row it
    // pans the board and never zooms it (the camera's wheelPansSelector).
    // Native, since the board's wheel listens natively on the overview's
    // root.
    useEffect(() => {
        const grid = gridRef.current;
        if (!grid) return undefined;
        const stop = (e: Event) => e.stopPropagation();
        grid.addEventListener('wheel', stop, { passive: true });
        return () => grid.removeEventListener('wheel', stop);
    }, [mounted]);

    // Bring a card into view once (a walk to it, a remove onto the shelf).
    useLayoutEffect(() => {
        if (!scrollTo || !mounted) return;
        const grid = gridRef.current;
        const card = grid?.querySelector<HTMLElement>(`[data-rm-card="${CSS.escape(scrollTo)}"]`);
        if (!grid || !card) return;
        useCollimateStore.getState().takeShelfScroll();
        if (card.offsetTop < grid.scrollTop || card.offsetTop + 60 > grid.scrollTop + grid.clientHeight) {
            grid.scrollTop = card.offsetTop - 14;
        }
    }, [scrollTo, mounted, entries]);

    const onCardClick = (cellId: string, e: React.MouseEvent) => {
        const last = lastClick.current;
        lastClick.current = null;
        // A double click is the two clicks that follow a card's lines; it
        // acts on the card its first click hit.
        if (e.detail > 1 && last && e.timeStamp - last.t < DOUBLE_CLICK_MS) {
            if (!last.followed) {
                pickShelf(last.id);
                anchorGhost(last.id);
            }
            return;
        }
        const state = useCollimateStore.getState();
        lastClick.current = {
            t: e.timeStamp, id: cellId,
            followed: !connectedOn(state.overview) || state.overview.picked === cellId,
        };
        pickShelf(cellId);
    };

    // The shelf's tags that match what is typed, the chips already chosen left out.
    const suggestions = useMemo(() => {
        if (!query.trim()) return [];
        return mergeVocabulary([], removedList, [])
            .filter(tag => tagMatches(query, tag) && !hasTag(tagFilters, tag))
            .slice(0, TAG_SUGGESTIONS_MAX);
    }, [query, removedList, tagFilters]);
    const suggesting = searchFocused && suggestions.length > 0;
    const active = Math.min(activeTag, Math.max(0, suggestions.length - 1));
    useLayoutEffect(() => {
        const label = searchRef.current;
        if (!suggesting || !label) return;
        setListBox(box => (box && box.left === label.offsetLeft && box.width === label.offsetWidth
            ? box : { left: label.offsetLeft, width: label.offsetWidth }));
    }, [suggesting, paneInset.left]);

    const chooseTag = (tag: string) => {
        addTagFilter(tag);
        setRemovedQuery('');
        setActiveTag(0);
    };

    const onSearchKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'Escape') {
            e.stopPropagation();
            if (query) setRemovedQuery(''); else e.currentTarget.blur();
        } else if (e.key === 'Enter' && suggesting) {
            e.preventDefault();
            chooseTag(suggestions[active]);
        } else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && suggesting) {
            e.preventDefault();
            const step = e.key === 'ArrowDown' ? 1 : -1;
            setActiveTag((active + step + suggestions.length) % suggestions.length);
        } else if (e.key === 'Backspace' && !e.currentTarget.value && tagFilters.length > 0) {
            e.preventDefault();
            removeTagFilter(tagFilters[tagFilters.length - 1]);
        }
    };

    const onRestore = async (cellId: string) => {
        setRestoring(cellId);
        setFailure(null);
        const ok = await restoreRemoved(cellId);
        setRestoring(null);
        if (!ok) setFailure(useCollimateStore.getState().overviewData.error ?? 'Restore failed.');
    };

    const count = (query.trim() || narrowed || tagFilters.length > 0 ? `${entries.length} of ` : '') + `${total} removed`;
    const empty = total === 0
        ? 'Nothing has been removed.'
        : narrowed && subject
            ? `No removed cell is connected to ${subjectName || 'this cell'}.`
            : 'No removed cell matches.';

    return (
        <div
            ref={shelfRef}
            data-rm-shelf
            data-drop-target={dropHere ? (dropHere.refusal ? 'refused' : 'on') : undefined}
            title={dropHere?.refusal ?? refused ?? undefined}
            data-overview-overlay
            role="region"
            aria-label="Removed cells"
            aria-hidden={!shelfOpen || undefined}
            inert={!shelfOpen}
            className="absolute left-0 top-0 z-[5] flex cursor-default flex-col overflow-hidden bg-canvas"
            style={{
                right: journalOpen ? JOURNAL_PANE_WIDTH : 0,
                marginRight: journalOpen ? paneInset.right : 0,
                transition: paneInsetTransition(['margin-right'], reduceMotion),
                height: 'var(--ov-shelf, 0px)',
            }}
            onPointerDown={(e) => e.stopPropagation()}
        >
            {mounted && (
                <>
                    <div
                        data-rm-controls
                        className="flex h-9 flex-none items-center gap-2 overflow-hidden whitespace-nowrap border-b border-line"
                        style={{
                            paddingLeft: SHELF_PAD + paneInset.left,
                            paddingRight: 11 + paneRight,
                            transition: paneInsetTransition(['padding-left', 'padding-right'], reduceMotion),
                        }}
                    >
                        <span className="text-[0.75rem] text-ink-3">Sort by</span>
                        <Segmented<RemovedSort>
                            label="Sort by"
                            value={sort}
                            options={[['created', 'Created'], ['edited', 'Last edited'], ['removed', 'Last removed']]}
                            onChange={setRemovedSort}
                        />
                        <span className="text-[0.75rem] text-ink-3">Order</span>
                        <Segmented<SortDir>
                            label="Order"
                            value={dir}
                            options={[['asc', 'Asc'], ['desc', 'Desc']]}
                            onChange={setRemovedDir}
                        />
                        <label ref={searchRef} className="flex h-[1.625rem] w-[15rem] min-w-[7.5rem] flex-[0_1_15rem] cursor-text items-center gap-1.5 rounded-md border border-line bg-sheet px-[0.5625rem] text-ink-3 focus-within:border-line-2">
                            <Search className="h-3.5 w-3.5 flex-none" strokeWidth={1.6} />
                            <input
                                type="text"
                                spellCheck={false}
                                autoComplete="off"
                                placeholder="Search removed cells"
                                aria-label="Search removed cells"
                                aria-controls={suggesting ? suggestionsId : undefined}
                                aria-activedescendant={suggesting ? `${suggestionsId}-${active}` : undefined}
                                value={query}
                                className="h-full min-w-0 flex-1 border-0 bg-transparent p-0 text-[0.78125rem] text-ink outline-none placeholder:text-ink-3"
                                onChange={(e) => { setRemovedQuery(e.target.value); setActiveTag(0); }}
                                onFocus={() => setSearchFocused(true)}
                                onBlur={() => setSearchFocused(false)}
                                onKeyDown={onSearchKey}
                            />
                        </label>
                        {tagFilters.map(tag => (
                            <span
                                key={tag}
                                data-rm-tag-filter={tag}
                                // A filter that is on: teal, the skin's on-state.
                                className="inline-flex h-[1.375rem] max-w-[10rem] flex-none items-center gap-1 rounded-sm border border-teal/50 bg-teal-soft pl-1.5 pr-0.5 text-[0.75rem] text-ink"
                            >
                                <Tag className="h-3 w-3 flex-none text-teal" strokeWidth={1.6} />
                                <span className="truncate">{tag}</span>
                                <button
                                    type="button"
                                    aria-label={`Stop filtering by ${tag}`}
                                    title="Stop filtering by this tag"
                                    className="inline-flex h-4 w-4 flex-none items-center justify-center rounded-xs text-ink-2 hover:bg-sheet hover:text-ink"
                                    onClick={() => removeTagFilter(tag)}
                                >
                                    <X className="h-3 w-3" strokeWidth={1.6} />
                                </button>
                            </span>
                        ))}
                        <button
                            type="button"
                            data-ov-show-tags
                            aria-label="Show tags on cards"
                            title="Show tags on cards"
                            aria-pressed={showTags}
                            className={cn(
                                'inline-flex h-6 w-6 flex-none items-center justify-center rounded-sm transition-colors',
                                showTags ? 'bg-sheet-2 text-ink' : 'text-ink-2 hover:bg-sheet-2 hover:text-ink',
                            )}
                            onClick={() => useCollimateStore.getState().setShowTags(!showTags)}
                        >
                            <Tag className="h-3.5 w-3.5" strokeWidth={1.6} />
                        </button>
                        <button
                            ref={zonesButtonRef}
                            type="button"
                            data-rm-zones
                            aria-label="Remove zones"
                            title="Remove zones"
                            aria-haspopup="dialog"
                            aria-expanded={zonesOpen}
                            className={cn(
                                'inline-flex h-6 w-6 flex-none items-center justify-center rounded-sm transition-colors',
                                zonesOpen ? 'bg-sheet-2 text-ink' : 'text-ink-2 hover:bg-sheet-2 hover:text-ink',
                            )}
                            onClick={() => setZonesOpen(open => !open)}
                        >
                            <SlidersHorizontal className="h-3.5 w-3.5" strokeWidth={1.6} />
                        </button>
                        <RemoveZonesEditor anchor={zonesButtonRef} open={zonesOpen} onClose={() => setZonesOpen(false)} />
                        <span data-rm-count className="text-[0.75rem] text-ink-3">{loaded ? count : ''}</span>
                        {failure && <span role="alert" className="truncate text-[0.75rem] text-red">{failure}</span>}
                    </div>
                    <div
                        ref={gridRef}
                        {...drag.gridHandlers}
                        data-rm-grid
                        className="relative grid min-h-0 flex-1 select-none content-start items-start overflow-y-auto py-3.5"
                        style={{
                            gap: SHELF_GAP,
                            paddingLeft: SHELF_PAD,
                            paddingRight: SHELF_PAD,
                            marginLeft: paneInset.left,
                            marginRight: paneRight,
                            transition: paneInsetTransition(['margin-left', 'margin-right'], reduceMotion),
                            gridTemplateColumns: columns,
                            // Cards fit the shelf: its height less the bar,
                            // the control row and the grid's padding.
                            ['--rm-ch' as string]: `clamp(130px, calc(var(--ov-shelf, 0px) - 4rem), ${OVERVIEW_CARD_HEIGHT}px)`,
                        }}
                    >
                        {loaded && entries.length === 0 && (
                            <div data-rm-empty className="col-span-full py-1.5 text-[0.8125rem] text-ink-3">{empty}</div>
                        )}
                        {shown.map(entry => (
                            <RemovedCard
                                key={entry.cell_id}
                                entry={entry}
                                sort={sort}
                                selected={entry.cell_id === selected}
                                journalStyle={journalOpen && entry.cell_id === shelfSubject}
                                followHint={entry.cell_id === hinted}
                                restoring={restoring === entry.cell_id}
                                onClick={(e) => onCardClick(entry.cell_id, e)}
                                onRestore={() => { void onRestore(entry.cell_id); }}
                            />
                        ))}
                    </div>
                    {suggesting && (
                        <ul
                            id={suggestionsId}
                            role="listbox"
                            aria-label="Matching tags"
                            data-rm-tag-suggestions
                            className="absolute z-[2] mt-0.5 max-h-[calc(100%-2.75rem)] list-none overflow-y-auto rounded-md border border-line-2 bg-sheet p-1 text-[0.78125rem] shadow-(--shadow)"
                            style={{ top: '2.25rem', left: listBox?.left ?? 0, width: listBox?.width ?? 240 }}
                        >
                            {suggestions.map((tag, i) => (
                                <li
                                    key={tag}
                                    id={`${suggestionsId}-${i}`}
                                    role="option"
                                    aria-selected={i === active}
                                    data-rm-tag-suggestion={tag}
                                    className={cn(
                                        'flex h-[1.625rem] cursor-pointer items-center gap-2 truncate rounded-sm px-2 text-ink-2',
                                        i === active && 'bg-sheet-2 text-ink',
                                    )}
                                    // Keep the search focused: the click adds the chip.
                                    onMouseDown={(e) => e.preventDefault()}
                                    onMouseEnter={() => setActiveTag(i)}
                                    onClick={() => chooseTag(tag)}
                                >
                                    <Tag className="h-3 w-3 flex-none text-ink-3" strokeWidth={1.6} />
                                    <span className="truncate">{tag}</span>
                                </li>
                            ))}
                        </ul>
                    )}
                    {dropHere && !dropHere.refusal && (
                        <ZoneStrip
                            zones={removeZones}
                            labelled
                            lit={dropHere.zone}
                            variant="shelf"
                            style={{ left: paneInset.left, right: paneRight }}
                        />
                    )}
                    {liftedEntry && <ShelfGhost drag={drag} entry={liftedEntry} sort={sort} />}
                </>
            )}
        </div>
    );
}
