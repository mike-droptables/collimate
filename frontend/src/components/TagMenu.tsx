// The tag menu (TG-05, TG-07): a cell's tags, edited from a Tag button in
// its head (the main-view card's tools row, left of the Move grip; the
// overview card's head, before Remove; a Removed shelf card's head, before
// Restore). Every cell carries tags, Capabilities cells included (a
// built-in card takes no metadata, so its hosts leave it off).
//
// The trigger shows the Tag glyph and, past zero, the count. Its right edge
// is the one Menu watches (align end): the hosts right-anchor it, so a count
// appearing grows it leftwards and the menu stays open.
//
// The menu (a ui/Menu role=dialog): the search box, a rule, this cell's
// tags as chips (x removes), the channel's tags matching the query as
// toggle rows, then "Create “x”" when nothing matches it exactly. ↑↓ move
// the active row, Enter runs it (by default the exact match, which Enter
// leaves be when the cell holds it, else the first match the cell lacks,
// else Create) and clears the box; Esc closes. Every change hands the whole
// list to the host's `onSave`: a board cell's goes through lib/cellTags
// (CellTagMenu), a shelf cell's through the store's setRemovedTags (its
// removed record); both are optimistic and rolled back on a refusal. The
// vocabulary is every stack's tags (Capabilities included, shown or
// hidden), the loaded shelf's and the vocabulary route's, fetched on open.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Plus, Search, Tag, X } from 'lucide-react';
import type { Cell } from '@/lib/types';
import { Menu } from '@/components/ui/Menu';
import { menuItem, menuSep } from '@/components/ui/menuStyles';
import { iconBtn } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { fetchChannelTags, type ChannelTag } from '@/lib/api/cells';
import { saveCellTags } from '@/lib/cellTags';
import {
    TAG_MAX_LENGTH, TAGS_PER_CELL_MAX, foldTag, hasTag, mergeVocabulary, normalizeTag, tagMatches,
} from '@/lib/tags';

const NO_TAGS: string[] = [];

export interface TagMenuProps {
    /** The cell's channel (its immutable id): the vocabulary's. */
    channel: string;
    /** The cell's tags as the host shows them. */
    tags: string[];
    /** Make `next` the cell's whole list; `prior` is the list the menu
     *  showed, put back if the save is refused. */
    onSave: (next: string[], prior: string[]) => void;
}

/** The tag menu of a cell on the board (its layout cell, edited through the
 *  meta route). */
export function CellTagMenu({ channel, cell }: { channel: string; cell: Cell }) {
    // The cache holds an optimistic edit before the layout catches up.
    const cached = useCollimateStore(s => s.cellCache[cell.id]?.tags);
    const tags = cached ?? cell.tags ?? NO_TAGS;
    return (
        <TagMenu
            channel={channel}
            tags={tags}
            onSave={(next, prior) => { void saveCellTags(channel, cell.id, next, prior); }}
        />
    );
}

export function TagMenu({ channel, tags, onSave }: TagMenuProps) {
    const [open, setOpen] = useState(false);
    const triggerRef = useRef<HTMLButtonElement>(null);

    return (
        // React synthetic events bubble through the Menu's portal along the
        // React tree: a click in the menu (its body or its padding) must not
        // reach the card's onClick, which selects the card.
        <span className="flex flex-none" onClick={(e) => e.stopPropagation()}>
            <button
                ref={triggerRef}
                type="button"
                aria-label="Tags"
                title="Tags"
                aria-haspopup="dialog"
                aria-expanded={open}
                data-tag-menu-trigger=""
                // Neither select the card (the camera would move the anchor
                // and close the menu) nor start a drag or a pan.
                onPointerDown={(e) => { if (e.button === 0) e.stopPropagation(); }}
                onClick={() => setOpen(o => !o)}
                className={cn(iconBtn, 'h-6 min-w-6 gap-0.5 px-1', open && 'bg-sheet-2 text-ink')}
            >
                <Tag className="h-3.5 w-3.5" strokeWidth={1.6} />
                {tags.length > 0 && <span className="text-[0.6875rem] leading-none tabular-nums">{tags.length}</span>}
            </button>
            <Menu
                anchor={triggerRef}
                open={open}
                onClose={() => setOpen(false)}
                role="dialog"
                label="Tags"
                align="end"
                minWidth={248}
                maxWidth={320}
            >
                <TagMenuBody channel={channel} tags={tags} onSave={onSave} />
            </Menu>
        </span>
    );
}

type Option = { kind: 'toggle' | 'create'; tag: string };

function TagMenuBody({ channel, tags, onSave }: TagMenuProps) {
    const [query, setQuery] = useState('');
    // null: the default row (the first that adds while something is typed).
    const [cursor, setCursor] = useState<number | null>(null);
    const [fetched, setFetched] = useState<ChannelTag[] | null>(null);
    const inputRef = useRef<HTMLInputElement>(null);
    const activeChannel = useCollimateStore(s => s.activeChannel);
    const cellCache = useCollimateStore(s => s.cellCache);
    const removed = useCollimateStore(s => s.overviewData.removed);

    useEffect(() => {
        let live = true;
        fetchChannelTags(channel)
            .then(rows => { if (live) setFetched(rows); })
            .catch((e: unknown) => console.error('Loading the channel tags failed', e));
        return () => { live = false; };
    }, [channel]);

    const vocabulary = useMemo(() => {
        const stacks = [...(activeChannel?.main_stacks ?? []), ...(activeChannel?.nav_stacks ?? [])];
        const board = stacks.flatMap(stack => stack.cells.map(c => ({ tags: cellCache[c.id]?.tags ?? c.tags })));
        return mergeVocabulary([{ tags }, ...board], removed, fetched);
    }, [activeChannel, cellCache, removed, fetched, tags]);

    const typed = normalizeTag(query);
    const full = tags.length >= TAGS_PER_CELL_MAX;
    const options = useMemo<Option[]>(() => {
        const matches = vocabulary.filter(t => tagMatches(query, t));
        // An exact match leads, so Enter takes what was typed.
        const exact = typed === null ? -1 : matches.findIndex(t => foldTag(t) === foldTag(typed));
        if (exact > 0) matches.unshift(...matches.splice(exact, 1));
        const rows: Option[] = matches.map(tag => ({ kind: 'toggle', tag }));
        if (typed !== null && exact === -1 && !full) rows.push({ kind: 'create', tag: typed });
        return rows;
    }, [vocabulary, query, typed, full]);

    const adds = (o: Option) => o.kind === 'create' || (!full && !hasTag(tags, o.tag));
    // What was typed exactly is the default row, even one the cell holds
    // (Enter then leaves it be); otherwise the first row that adds.
    const lead = options[0];
    const exactLeads = typed !== null && lead?.kind === 'toggle' && foldTag(lead.tag) === foldTag(typed);
    const active = cursor !== null && cursor < options.length
        ? cursor
        : (typed === null ? -1 : exactLeads ? 0 : options.findIndex(adds));

    // A refusal puts back the list this menu showed.
    const save = (next: string[]) => onSave(next, tags);
    const remove = (tag: string) => save(tags.filter(t => foldTag(t) !== foldTag(tag)));
    const run = (o: Option) => {
        if (o.kind === 'toggle' && hasTag(tags, o.tag)) remove(o.tag);
        else if (!full && !hasTag(tags, o.tag)) save([...tags, o.tag]);
    };
    const clear = () => { setQuery(''); setCursor(null); };

    const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.nativeEvent.isComposing) return;
        const n = options.length;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            if (n === 0) return;
            const step = e.key === 'ArrowDown' ? 1 : -1;
            setCursor(active === -1 ? (step === 1 ? 0 : n - 1) : (active + step + n) % n);
        } else if (e.key === 'Enter') {
            // Marked handled, so the Menu's own Enter leaves it alone.
            e.preventDefault();
            const o = options[active];
            if (!o) return;
            // The default row runs only when it adds; a row the arrows chose
            // toggles, as its click does.
            if (cursor !== null || adds(o)) run(o);
            clear();
        }
    };

    const note = full
        ? `A cell holds at most ${TAGS_PER_CELL_MAX} tags`
        : query.trim() && typed === null
            ? `A tag is at most ${TAG_MAX_LENGTH} characters, with no control characters`
            : null;

    return (
        <div className="flex flex-col" data-tag-menu="">
            <div className="flex h-[1.875rem] items-center gap-2 px-2.5">
                <Search className="h-3.5 w-3.5 flex-none text-ink-3" strokeWidth={1.6} />
                <input
                    ref={inputRef}
                    // Menu also puts the keyboard here, its first control.
                    autoFocus
                    value={query}
                    onChange={(e) => { setQuery(e.target.value); setCursor(null); }}
                    onKeyDown={onKeyDown}
                    placeholder="Search or create a tag"
                    aria-label="Search or create a tag"
                    spellCheck={false}
                    className="min-w-0 flex-1 bg-transparent text-[0.8125rem] text-ink outline-none placeholder:text-ink-3"
                />
            </div>
            <div role="separator" className={menuSep} />
            {tags.length > 0 ? (
                <ul aria-label="This cell's tags" className="m-0 flex list-none flex-wrap gap-1 px-2 py-1.5">
                    {tags.map(tag => (
                        <li
                            key={tag}
                            className="inline-flex h-[1.375rem] max-w-full items-center gap-0.5 rounded-sm border border-line bg-sheet-2 pl-1.5 pr-0.5 text-[0.75rem] text-ink-2"
                        >
                            <span className="truncate">{tag}</span>
                            <button
                                type="button"
                                aria-label={`Remove ${tag}`}
                                title="Take this tag off"
                                onClick={() => { remove(tag); inputRef.current?.focus(); }}
                                className="flex h-4 w-4 flex-none items-center justify-center rounded-xs text-ink-3 transition-colors hover:bg-sheet hover:text-ink"
                            >
                                <X className="h-3 w-3" strokeWidth={1.8} />
                            </button>
                        </li>
                    ))}
                </ul>
            ) : (
                <div className="px-2.5 py-1.5 text-[0.75rem] text-ink-3">No tags yet</div>
            )}
            {options.length > 0 && (
                <div aria-label="Channel tags" role="group" className="max-h-[15rem] overflow-y-auto">
                    {options.map((o, i) => {
                        const on = o.kind === 'toggle' && hasTag(tags, o.tag);
                        const inert = !on && !adds(o);
                        return (
                            <button
                                key={`${o.kind}:${o.tag}`}
                                type="button"
                                data-tag-option={o.kind}
                                aria-pressed={o.kind === 'toggle' ? on : undefined}
                                aria-disabled={inert || undefined}
                                data-active={i === active || undefined}
                                title={inert ? `A cell holds at most ${TAGS_PER_CELL_MAX} tags` : undefined}
                                onClick={() => {
                                    if (inert) return;
                                    run(o);
                                    if (o.kind === 'create') clear();
                                    inputRef.current?.focus();
                                }}
                                className={cn(
                                    menuItem,
                                    i === active && 'bg-sheet-2 text-ink',
                                    on && 'text-ink [&_svg]:text-brass',
                                    // An inert row keeps its pointer for the title.
                                    inert && 'aria-disabled:pointer-events-auto cursor-default hover:bg-transparent hover:text-ink-2',
                                )}
                            >
                                {o.kind === 'create'
                                    ? <Plus className="h-3.5 w-3.5 flex-none" strokeWidth={1.6} />
                                    : <Check className={cn('h-3.5 w-3.5 flex-none', !on && 'invisible')} strokeWidth={1.8} />}
                                <span className="truncate">{o.kind === 'create' ? `Create “${o.tag}”` : o.tag}</span>
                            </button>
                        );
                    })}
                </div>
            )}
            {note && <div className="px-2.5 py-1.5 text-[0.75rem] text-ink-3">{note}</div>}
        </div>
    );
}
