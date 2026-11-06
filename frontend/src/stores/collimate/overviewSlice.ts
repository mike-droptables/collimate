import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import { isCapabilitiesStack, type Cell, type ChannelLayout, type Stack } from '@/lib/types';
import { describeApiError } from '@/lib/api/client';
import { moveCellTo, moveStackTo } from '@/lib/api/stacks';
import {
    fetchJournal, fetchLineage, fetchRemovedCells, putRemovedCellTags, removeCellById, restoreRemovedCell, JOURNAL_PAGE_LIMIT,
    type JournalEvent, type Lineage, type LineageMove, type RemovedCell, type RemovedSort, type SortDir,
} from '@/lib/api/cells';
import type { RemoveZone, ZoneAction } from '@/lib/api/settings';
import {flushCellDisplays} from '@/lib/cellDisplayWrites';
import { cellHasLiveRun, isRunPath } from '@/lib/runningCell';
import { foldTag, hasTag, normalizeTag, normalizeTags } from '@/lib/tags';
import { applyZoneAction, removeBodyOf } from '@/components/overview/removeZones';

// The overview (zoom-out): the board of stacks, the Removed shelf above it,
// the journal pane on its right and the lineage arrows over it. This slice
// holds what those share: the view state (the mock's `ovs`), the data the
// routes serve, and the two-click anchor rule (the mock's anchorSubject,
// armedBoard and walkTo). Every cell is named by its id, on the board or on
// the shelf: a cell keeps its id when it is removed and restored.

// ── Geometry the overview's parts share (the mock's px) ───────────────
/** The Removed bar on the board's top edge. */
export const OVERVIEW_BAR_HEIGHT = 33;
/** The journal pane on the right, while the journal is open. */
export const JOURNAL_PANE_WIDTH = 374;
/** A board column (a stack) and the gap between columns and cards. */
export const OVERVIEW_COLUMN_WIDTH = 299;
export const OVERVIEW_GAP = 20;
/** A card at rest; a journal-style card grows from it. */
export const OVERVIEW_CARD_HEIGHT = 273;
/** The camera's zoom range in the overview. */
export const OVERVIEW_ZOOM = { min: 0.2, max: 1.5 } as const;
/** The zoom control's step (+ multiplies by it, − divides), and a plain
 *  wheel's per 100px notch. */
export const OVERVIEW_ZOOM_STEP = 1.25;

/** The board's region inside the overview (overview-local px): below the
 *  shelf's current (animated) height and the bar, left of the journal pane
 *  when it is open. */
export const boardRegion = (overview: { width: number; height: number }, shelfPx: number, journalOpen: boolean) => ({
    left: 0,
    top: shelfPx + OVERVIEW_BAR_HEIGHT,
    right: overview.width - (journalOpen ? JOURNAL_PANE_WIDTH : 0),
    bottom: overview.height,
});

/** The shelf's height when it first opens (the mock's `ovs.h`). */
export const SHELF_HEIGHT_DEFAULT = 348;
/** A drag that leaves the shelf shorter than this shuts it. */
export const SHELF_MIN_OPEN = 110;
/** The journal key of the All cells state (every cell's journal). */
export const ALL_CELLS = '*';
/** The search waits this long after the last key. */
export const SHELF_SEARCH_MS = 200;

/** Why a board cell cannot be removed while it runs (the Remove button's
 *  title, and a refused drop's). */
export const REMOVE_RUNNING = 'Stop its runs to remove it';

/** Where a lifted board card can be let go to be removed. */
export type ShelfZone = 'shelf' | 'bar';

/** Where a restored shelf cell lands: a new column at board slot `slot`
 *  (the board's column order), or stack `stackId` at stack position
 *  `index`. */
export type RestoreTarget =
    | { kind: 'column'; slot: number }
    | { kind: 'stack'; stackId: string; index: number };

/** A card lifted across the line between the board and the shelf.
 *  `remove`: a removable board card, over the shelf or the bar (`over`;
 *  null while it is still over the board, the bar showing the zones' labels
 *  all the same), over remove zone `zone` (an index into the effective
 *  zones; null over the board or when no zone strip is on show), refused
 *  while it runs (`refusal`, over the shelf or the bar). `restore`: a shelf card, with the pointer
 *  over the board (`point`, window px, else null) and where it lands
 *  there (`target`, written by the board's drag). */
export type OverviewDrop =
    | { kind: 'remove'; cellId: string; over: ShelfZone | null; zone: number | null; refusal: string | null }
    | { kind: 'restore'; cellId: string; point: { x: number; y: number } | null; target: RestoreTarget | null };

/** A refused drop: the target it was let go on shakes, its title the reason. */
export interface DropRefusal {
    reason: string;
    at: ShelfZone;
    nonce: number;
}

/** The overview's view state. Cell ids throughout. */
export interface OverviewView {
    shelfOpen: boolean;
    /** The open shelf's height in px, per channel in the settings document
     *  (`layout.removedShelfHeight`). */
    shelfHeight: number;
    sort: RemovedSort;
    dir: SortDir;
    query: string;
    /** The shelf's tag filter chips: a card shows when it carries every one
     *  (case-folded). Not saved; a channel switch clears them. */
    tagFilters: string[];
    /** The channel's remove zones, left to right (the settings document's
     *  `remove_zones`; empty: one full-width plain remove). */
    removeZones: RemoveZone[];
    /** Every board card shows its cell's tags in its body. Per channel in
     *  the settings document (`layout.overviewShowTags`). */
    showTags: boolean;
    /** The shelf card that carries Restore. */
    shelfSelection: string | null;
    /** The shelf card clicked last: a second click on it follows its lines. */
    picked: string | null;
    /** The cell whose journal is open and whose lines are drawn, on the
     *  board or on the shelf. Null: the selected board cell. */
    anchor: string | null;
    journalOpen: boolean;
    onlyConnected: boolean;
    /** Clear was pressed (Only connected on): no subject, the whole board
     *  and the All cells journal, until a cell is anchored again. */
    cleared: boolean;
    /** After Clear, the board card clicked once (its second click anchors). */
    boardPick: string | null;
    /** A shelf card to bring into view once (`takeShelfScroll`). */
    scrollTo: string | null;
    /** A card lifted between the board and the shelf. */
    drop: OverviewDrop | null;
    /** The last refused drop. */
    refusal: DropRefusal | null;
}

/** A journal cache entry: the pages loaded so far, newest first. */
export interface JournalPage {
    events: JournalEvent[];
    loading: boolean;
    /** The oldest event is loaded. */
    complete: boolean;
    error: string | null;
}

/** The routes' data for the channel the overview shows. */
export interface OverviewData {
    channel: string | null;
    /** Every removed cell on the shelf, in the view's sort; null until loaded. */
    removed: RemovedCell[] | null;
    /** The ids the server matched for `query`; null without a query. */
    removedMatches: string[] | null;
    lineage: Lineage | null;
    /** By cell id, and `ALL_CELLS`. */
    journals: Record<string, JournalPage>;
    /** The journal key the pane shows: refreshed in place on a change,
     *  where every other affected key is dropped. */
    journalShown: string | null;
    /** The last failed load or action, for the surface that made it. */
    error: string | null;
}

export interface OverviewSlice {
    overview: OverviewView;
    overviewData: OverviewData;

    setShelfOpen: (open: boolean) => void;
    /** A resize's end: below SHELF_MIN_OPEN the shelf shuts (its height is
     *  kept), else the height is taken and the shelf is open. */
    setShelfHeight: (px: number) => void;
    /** A channel's saved height (its settings document loaded); no save. */
    adoptShelfHeight: (px: number) => void;
    setRemovedSort: (sort: RemovedSort) => void;
    setRemovedDir: (dir: SortDir) => void;
    /** Matches are fetched SHELF_SEARCH_MS after the last call. */
    setRemovedQuery: (query: string) => void;
    /** A tag filter chip (normalised; one per case-folded spelling). */
    addTagFilter: (tag: string) => void;
    removeTagFilter: (tag: string) => void;
    clearTagFilters: () => void;
    /** A channel's saved remove zones (its settings document loaded); no save. */
    adoptRemoveZones: (zones: RemoveZone[]) => void;
    /** The zones the editor set; useOverviewPersistence saves them. */
    setRemoveZones: (zones: RemoveZone[]) => void;
    /** The tag toggle; useOverviewPersistence saves it. */
    setShowTags: (on: boolean) => void;
    /** A channel's saved tag toggle (its settings document loaded); no save. */
    adoptShowTags: (on: boolean) => void;
    setJournalOpen: (open: boolean) => void;

    /** A click on a board card, before it selects: anchor it when Only
     *  connected is off or it was already selected (armed), else after
     *  Clear remember it as clicked once. Then select it. */
    selectCard: (cellId: string) => void;
    /** A click on a shelf card: it carries Restore; it is anchored when Only
     *  connected is off or it was the card clicked last. */
    pickShelf: (cellId: string) => void;
    anchorTo: (cellId: string) => void;
    anchorGhost: (cellId: string) => void;
    /** Clear (Only connected on): no subject until a cell is anchored. */
    clearAnchor: () => void;
    toggleConnected: () => void;
    /** A name in the journal or on an arrow: a board cell is clicked
     *  (`selectCard`), a shelf cell is picked, shown and scrolled to (the
     *  search clears when it hides the cell). */
    walkTo: (cellId: string) => void;
    /** The shelf card to scroll into view, once. */
    takeShelfScroll: () => string | null;

    /** Load the shelf and the lineage for `channel` (a new channel drops
     *  the previous one's data and anchor). */
    loadOverview: (channel: string) => Promise<void>;
    /** Forget the data (the overview closed); the view state stays. */
    dropOverviewData: () => void;
    /** Show a journal key (a cell id, ALL_CELLS, or null for none), loading
     *  its first page when it is not cached. */
    showJournal: (key: string | null) => Promise<void>;
    /** The next older page of a journal key. */
    loadMoreJournal: (key: string) => Promise<void>;
    /** Restore a shelf cell, selected and revealed: at the board's right
     *  end, or at `target` (a new column, or a stack at a position). It
     *  leaves the shelf at once and comes back if the restore fails. False
     *  when refused or failed (`overviewData.error`). */
    restoreRemoved: (cellId: string, target?: RestoreTarget) => Promise<boolean>;
    /** Remove a board cell onto the shelf, where it is selected; the next
     *  board cell is selected. `action`: the remove zone's, applied to the
     *  removed record's tags (the live cell is never edited). Its card is on
     *  the shelf at once, carrying the tags the action leaves, and leaves it
     *  if the removal fails. False for a running cell, a Capabilities cell,
     *  or a failure. */
    removeFromBoard: (cellId: string, action?: ZoneAction) => Promise<boolean>;
    /** Make `next` a shelf cell's whole tag list (its removed record): the
     *  card shows it at once; the server's normalized list replaces it, and
     *  a refusal puts `prior` back (unless a later edit or a reread replaced
     *  this one). False when refused or failed (`overviewData.error`). A
     *  list the tag rules refuse throws before anything changes. */
    setRemovedTags: (cellId: string, next: string[], prior: string[]) => Promise<boolean>;
    /** A card lifted, moved or let go between the board and the shelf. */
    setOverviewDrop: (drop: OverviewDrop | null) => void;
    /** A drop refused on `at`: it shakes and says why. */
    refuseDrop: (reason: string, at: ShelfZone) => void;
    /** SSE lineage_changed: drop these cells' journals (and All cells),
     *  refresh the shown one, reload the shelf and the lineage when loaded. */
    overviewLineageChanged: (cellIds: string[]) => void;
    /** SSE run_started / run_status / dag_commit (a run's start, a version
     *  it writes mid-run, its end): drop the journals that hold the run, that
     *  belong to its cell or that name the cell (and All cells), refresh the
     *  shown one, so the open journal stays current while the run goes. */
    overviewRunChanged: (runId: string | null, cellId: string | null) => void;
    /** The stream (re)opened and may have missed events: reread whatever
     *  is loaded (the shelf, its matches, the lineage, the shown journal)
     *  and drop the other journals. */
    overviewResync: () => void;
}

export const defaultOverviewView = (): OverviewView => ({
    shelfOpen: false, shelfHeight: SHELF_HEIGHT_DEFAULT, sort: 'edited', dir: 'desc', query: '',
    tagFilters: [], removeZones: [], showTags: false,
    shelfSelection: null, picked: null, anchor: null, journalOpen: false, onlyConnected: false,
    cleared: false, boardPick: null, scrollTo: null, drop: null, refusal: null,
});

const emptyData = (channel: string | null): OverviewData => ({
    channel, removed: null, removedMatches: null, lineage: null, journals: {}, journalShown: null, error: null,
});

/** Move a restored column (`stackId`, at the board's right end of `stacks`,
 *  holding the restored `cellId`) to its drop `target`: a new column at a
 *  slot short of the end, or into a stack at a position (its own emptied
 *  column goes). Null when it stays where the restore put it. */
function placeRestored(
    channel: string, stacks: readonly { id: string }[], stackId: string, cellId: string, target: RestoreTarget | undefined,
): Promise<ChannelLayout | null> {
    const end = stacks.findIndex(stack => stack.id === stackId);
    if (!target || end < 0) return Promise.resolve(null);
    if (target.kind === 'column') {
        return target.slot >= 0 && target.slot < end
            ? moveStackTo(channel, stackId, target.slot, 'insert')
            : Promise.resolve(null);
    }
    const into = stacks.findIndex(stack => stack.id === target.stackId);
    return into >= 0 && into !== end
        ? moveCellTo(channel, stackId, into, 'stack', cellId, target.index)
        : Promise.resolve(null);
}

// ── Pure reads (selectors take the store state) ────────────────────────

type OverviewRead = Pick<CollimateState, 'overview' | 'overviewData' | 'selectedCellId' | 'activeChannel'>;

/** Only connected applies only while the journal is open. */
export const connectedOn = (view: OverviewView): boolean => view.journalOpen && view.onlyConnected;

/** Every board cell id, column by column, each column in its stack's order
 *  (the order the column shows and Remove steps through). */
export const boardOrder = (stacks: readonly Stack[]): string[] =>
    stacks.flatMap(stack => stack.cells.map(cell => String(cell.id)));

const boardStackOf = (s: OverviewRead, cellId: string): Stack | null =>
    s.activeChannel?.main_stacks.find(stack => stack.cells.some(cell => String(cell.id) === cellId)) ?? null;

/** Where a cell is: on the board, on the shelf, or nowhere the overview shows. */
export function cellPlace(s: OverviewRead, cellId: string): 'board' | 'shelf' | null {
    if (boardStackOf(s, cellId)) return 'board';
    return s.overviewData.removed?.some(entry => entry.cell_id === cellId) ? 'shelf' : null;
}

/** The journal's subject (the mock's anchorSubject): none after Clear; the
 *  anchor while it is on the board or the shelf; else the selected cell. */
export function subjectCellId(s: OverviewRead): string | null {
    const view = s.overview;
    if (view.cleared && connectedOn(view)) return null;
    if (view.anchor && cellPlace(s, view.anchor)) return view.anchor;
    return s.selectedCellId && cellPlace(s, s.selectedCellId) === 'board' ? s.selectedCellId : null;
}

/** The journal key the pane shows: the subject, else All cells after
 *  Clear, else none. */
export function journalKey(s: OverviewRead): string | null {
    if (!s.overview.journalOpen) return null;
    const subject = subjectCellId(s);
    if (subject) return subject;
    return s.overview.cleared && connectedOn(s.overview) ? ALL_CELLS : null;
}

/** With Only connected on, a click on a board card follows its lines only
 *  when that card was already selected; after Clear, once it was clicked. */
export function armedBoard(s: OverviewRead, cellId: string): boolean {
    const selected = s.selectedCellId === cellId;
    return s.overview.cleared ? s.overview.boardPick === cellId && selected : selected;
}

/** One line from the subject: the move, the cell at its other end, and
 *  whether the line leaves the subject (`out`: the subject is a source). */
export interface Relation {
    move: LineageMove;
    cellId: string;
    out: boolean;
}

/** The subject's direct duplicates, merges and splits, both ways (a run in
 *  a new cell draws no line). */
export function relationsOf(lineage: Lineage | null, subject: string | null): Relation[] {
    if (!lineage || !subject) return [];
    const out: Relation[] = [];
    for (const move of lineage.moves) {
        if (move.op === 'run_cell') continue;
        if (move.from.some(end => end.cell_id === subject)) {
            for (const end of move.to) out.push({ move, cellId: end.cell_id, out: true });
        }
        if (move.to.some(end => end.cell_id === subject)) {
            for (const end of move.from) out.push({ move, cellId: end.cell_id, out: false });
        }
    }
    return out;
}

/** The cells Only connected keeps: the subject and the cells it has lines
 *  to (a board cell selected among them stays). Null when it is off or
 *  there is no subject: everything shows. */
export function connectedCells(s: OverviewRead): Set<string> | null {
    const subject = subjectCellId(s);
    if (!connectedOn(s.overview) || !subject) return null;
    return new Set([subject, ...relationsOf(s.overviewData.lineage, subject).map(r => r.cellId)]);
}

/** A shelf card carries every one of `filters` (case-folded). */
export function carriesTags(entry: RemovedCell, filters: readonly string[]): boolean {
    if (filters.length === 0) return true;
    const tags = new Set(entry.tags.map(foldTag));
    return filters.every(tag => tags.has(foldTag(tag)));
}

/** The shelf's cards: the removed cells in the view's sort, narrowed by the
 *  search, the tag filter chips and Only connected. */
export function shelfEntries(s: OverviewRead): RemovedCell[] {
    const all = s.overviewData.removed ?? [];
    const matches = s.overviewData.removedMatches ? new Set(s.overviewData.removedMatches) : null;
    const keep = connectedCells(s);
    const filters = s.overview.tagFilters;
    return all.filter(entry => (!matches || matches.has(entry.cell_id)) && (!keep || keep.has(entry.cell_id))
        && carriesTags(entry, filters));
}

/** The selected board card says "Click again to follow its lines". */
export function boardFollowHint(s: OverviewRead, cellId: string): boolean {
    if (!connectedOn(s.overview) || s.selectedCellId !== cellId) return false;
    const subject = subjectCellId(s);
    if (!subject) return s.overview.boardPick === cellId;
    return cellPlace(s, subject) === 'shelf' || subject !== cellId;
}

/** The picked shelf card says "Click again to follow its lines". */
export const shelfFollowHint = (s: OverviewRead, cellId: string): boolean =>
    connectedOn(s.overview) && s.overview.picked === cellId && subjectCellId(s) !== cellId;

/** The time a shelf sort reads: made, last edited or last removed. */
const sortTime = (entry: RemovedCell, sort: RemovedSort): string | null =>
    sort === 'created' ? entry.created_at : sort === 'removed' ? entry.removed_at : entry.edited_at;

/** The server's shelf order: by created, edited or removed time (missing
 *  first in ascending order), ties by cell id ascending in either direction. */
export function sortRemoved(entries: readonly RemovedCell[], sort: RemovedSort, dir: SortDir): RemovedCell[] {
    const key = (entry: RemovedCell) => sortTime(entry, sort) ?? '';
    const sign = dir === 'asc' ? 1 : -1;
    return [...entries].sort((a, b) =>
        sign * key(a).localeCompare(key(b)) || a.cell_id.localeCompare(b.cell_id));
}

/** A board cell that can go onto the shelf: a standard stack's cell (a
 *  Capabilities cell has no Remove; it moves off first). */
export function removableCard(s: OverviewRead, cellId: string): boolean {
    const stack = boardStackOf(s, cellId);
    return stack !== null && !isCapabilitiesStack(stack);
}

/** A shelf card for a board cell, until the shelf route answers with its
 *  own: its tags as the remove zone's `action` leaves them. */
export function provisionalRemoved(cell: Cell, stack: Stack, removedAt: string, action?: ZoneAction): RemovedCell {
    const files = cell.files.map(f => f.name).filter(name => !isRunPath(name)).sort();
    return {
        cell_id: String(cell.id), tip_version_id: null, name: cell.name, icon: cell.icon ?? null,
        stack_name: stack.name || null, stack_cell_count: stack.cells.length, reason: 'removed',
        created_at: cell.created_at ?? null, edited_at: cell.updated_at ?? null, removed_at: removedAt,
        made_by: { kind: 'user', runner_id: null, runner_name: null },
        files, file_count: files.length, preview: null, tags: applyZoneAction(cell.tags ?? [], action),
    };
}

// ── The slice ──────────────────────────────────────────────────────────

export const createOverviewSlice: StateCreator<CollimateState, [], [], OverviewSlice> = (set, get, store) => {
    const view = (patch: Partial<OverviewView>) => set(prev => ({ overview: { ...prev.overview, ...patch } }));
    const data = (patch: Partial<OverviewData>) => set(prev => ({ overviewData: { ...prev.overviewData, ...patch } }));
    const channelOf = () => get().overviewData.channel;
    const anchorPatch = (cellId: string): Partial<OverviewView> => ({ anchor: cellId, cleared: false, boardPick: null });

    // Each load carries a token; a response whose token moved on (a newer
    // load, an invalidation, a channel switch) is dropped.
    let removedToken = 0;
    let matchesToken = 0;
    let lineageToken = 0;
    const journalTokens = new Map<string, number>();
    const bumpJournal = (key: string) => {
        const token = (journalTokens.get(key) ?? 0) + 1;
        journalTokens.set(key, token);
        return token;
    };
    let searchTimer: ReturnType<typeof setTimeout> | null = null;

    const reloadRemoved = async () => {
        const channel = channelOf();
        if (!channel) return;
        const token = ++removedToken;
        const { sort, dir } = get().overview;
        try {
            const removed = await fetchRemovedCells(channel, { sort, dir });
            if (token !== removedToken || channelOf() !== channel) return;
            const now = get().overview;
            data({ removed: sortRemoved(removed, now.sort, now.dir) });
        } catch (err) {
            if (token === removedToken && channelOf() === channel) data({ error: describeApiError(err) });
        }
    };

    const reloadMatches = async () => {
        const channel = channelOf();
        const query = get().overview.query.trim();
        const token = ++matchesToken;
        if (!channel || !query) {
            data({ removedMatches: null });
            return;
        }
        const { sort, dir } = get().overview;
        try {
            const found = await fetchRemovedCells(channel, { sort, dir, q: query });
            if (token !== matchesToken || channelOf() !== channel) return;
            data({ removedMatches: found.map(entry => entry.cell_id) });
        } catch (err) {
            if (token === matchesToken && channelOf() === channel) data({ error: describeApiError(err) });
        }
    };

    const reloadLineage = async () => {
        const channel = channelOf();
        if (!channel) return;
        const token = ++lineageToken;
        try {
            const lineage = await fetchLineage(channel);
            if (token !== lineageToken || channelOf() !== channel) return;
            data({ lineage });
        } catch (err) {
            if (token === lineageToken && channelOf() === channel) data({ error: describeApiError(err) });
        }
    };

    const setJournal = (key: string, patch: Partial<JournalPage>) => set(prev => {
        const current = prev.overviewData.journals[key]
            ?? { events: [], loading: false, complete: false, error: null };
        return { overviewData: { ...prev.overviewData, journals: { ...prev.overviewData.journals, [key]: { ...current, ...patch } } } };
    });

    /** Fetch a journal key: its first `limit` events (`replace`), or the
     *  page older than what is loaded. */
    const fetchJournalPage = async (key: string, mode: 'replace' | 'older', limit = JOURNAL_PAGE_LIMIT) => {
        const channel = channelOf();
        if (!channel) return;
        const before = mode === 'older' ? get().overviewData.journals[key]?.events.at(-1)?.t ?? null : null;
        const token = bumpJournal(key);
        setJournal(key, { loading: true, error: null });
        try {
            const page = await fetchJournal(channel, key === ALL_CELLS ? null : key, { before, limit });
            if (journalTokens.get(key) !== token || channelOf() !== channel) return;
            const loaded = mode === 'older' ? get().overviewData.journals[key]?.events ?? [] : [];
            setJournal(key, { events: [...loaded, ...page], loading: false, complete: page.length < limit });
        } catch (err) {
            if (journalTokens.get(key) === token && channelOf() === channel) {
                setJournal(key, { loading: false, error: describeApiError(err) });
            }
        }
    };

    /** Drop the affected journal keys; the shown one refetches in place, as
     *  many events as it holds (at most the route's 500). */
    const invalidateJournals = (affected: (key: string, page: JournalPage) => boolean) => {
        const { journals, journalShown } = get().overviewData;
        const next: Record<string, JournalPage> = {};
        let refresh: { key: string; limit: number } | null = null;
        let dropped = false;
        for (const [key, page] of Object.entries(journals)) {
            if (!affected(key, page)) {
                next[key] = page;
            } else if (key === journalShown) {
                next[key] = page;
                refresh = { key, limit: Math.min(500, Math.max(JOURNAL_PAGE_LIMIT, page.events.length)) };
            } else {
                bumpJournal(key);
                dropped = true;
            }
        }
        if (dropped) data({ journals: next });
        if (refresh) void fetchJournalPage(refresh.key, 'replace', refresh.limit);
    };

    // With Only connected off the anchor follows the selection, wherever it
    // comes from (a click, the keyboard, a restore). A channel switch drops
    // the previous channel's data and cell ids.
    store.subscribe((state, prev) => {
        if (state.selectedCellId !== prev.selectedCellId && !connectedOn(state.overview)) {
            view(state.selectedCellId ? anchorPatch(state.selectedCellId) : { anchor: null, cleared: false, boardPick: null });
        }
        const channel = state.activeChannel?.id ?? null;
        if (channel !== (prev.activeChannel?.id ?? null) && state.overviewData.channel
            && state.overviewData.channel !== channel) {
            journalTokens.clear();
            if (searchTimer !== null) clearTimeout(searchTimer);
            searchTimer = null;
            set({ overviewData: emptyData(null) });
            view({
                anchor: null, picked: null, shelfSelection: null, boardPick: null, cleared: false, scrollTo: null, query: '',
                tagFilters: [],
            });
        }
    });

    return {
        overview: defaultOverviewView(),
        overviewData: emptyData(null),

        setShelfOpen: (open) => view({ shelfOpen: open }),
        setShelfHeight: (px) => {
            if (px < SHELF_MIN_OPEN) view({ shelfOpen: false });
            else view({ shelfOpen: true, shelfHeight: Math.round(px) });
        },
        adoptShelfHeight: (px) => view({ shelfHeight: Math.max(SHELF_MIN_OPEN, Math.round(px)) }),
        setRemovedSort: (sort) => {
            view({ sort });
            const { removed } = get().overviewData;
            if (removed) data({ removed: sortRemoved(removed, sort, get().overview.dir) });
        },
        setRemovedDir: (dir) => {
            view({ dir });
            const { removed } = get().overviewData;
            if (removed) data({ removed: sortRemoved(removed, get().overview.sort, dir) });
        },
        setRemovedQuery: (query) => {
            view({ query });
            if (searchTimer !== null) clearTimeout(searchTimer);
            if (!query.trim()) {
                searchTimer = null;
                ++matchesToken;
                data({ removedMatches: null });
                return;
            }
            const channel = channelOf();
            searchTimer = setTimeout(() => { searchTimer = null; if (channelOf() === channel) void reloadMatches(); }, SHELF_SEARCH_MS);
        },
        addTagFilter: (tag) => {
            const clean = normalizeTag(tag);
            const filters = get().overview.tagFilters;
            if (!clean || hasTag(filters, clean)) return;
            view({ tagFilters: [...filters, clean] });
        },
        removeTagFilter: (tag) => {
            const filters = get().overview.tagFilters;
            const next = filters.filter(t => foldTag(t) !== foldTag(tag));
            if (next.length !== filters.length) view({ tagFilters: next });
        },
        clearTagFilters: () => {
            if (get().overview.tagFilters.length > 0) view({ tagFilters: [] });
        },
        adoptRemoveZones: (zones) => view({ removeZones: zones }),
        setRemoveZones: (zones) => view({ removeZones: zones }),
        setShowTags: (on) => view({ showTags: on }),
        adoptShowTags: (on) => view({ showTags: on }),
        setJournalOpen: (open) => view({ journalOpen: open }),

        selectCard: (cellId) => {
            const state = get();
            if (!connectedOn(state.overview) || armedBoard(state, cellId)) view(anchorPatch(cellId));
            else if (state.overview.cleared) view({ boardPick: cellId });
            state.requestSelectCell(cellId);
        },
        pickShelf: (cellId) => {
            const current = get().overview;
            const again = current.picked === cellId;
            view({
                shelfSelection: cellId, picked: cellId,
                ...(!connectedOn(current) || again ? anchorPatch(cellId) : {}),
            });
        },
        anchorTo: (cellId) => view(anchorPatch(cellId)),
        anchorGhost: (cellId) => view(anchorPatch(cellId)),
        clearAnchor: () => view({ cleared: true, anchor: null, picked: null, boardPick: null }),
        toggleConnected: () => {
            const on = !get().overview.onlyConnected;
            view({ onlyConnected: on, cleared: false, boardPick: null, ...(on ? { picked: null } : {}) });
        },
        walkTo: (cellId) => {
            const state = get();
            const place = cellPlace(state, cellId);
            if (place === 'board') {
                state.selectCard(cellId);
                return;
            }
            if (place !== 'shelf') return;
            const { removedMatches } = state.overviewData;
            if (state.overview.query && removedMatches && !removedMatches.includes(cellId)) {
                state.setRemovedQuery('');
            }
            const current = get().overview;
            view({
                shelfSelection: cellId, picked: cellId, scrollTo: cellId, shelfOpen: true,
                ...(!connectedOn(current) || current.picked === cellId ? anchorPatch(cellId) : {}),
            });
        },
        takeShelfScroll: () => {
            const target = get().overview.scrollTo;
            if (target !== null) view({ scrollTo: null });
            return target;
        },

        loadOverview: async (channel) => {
            if (channelOf() !== channel) {
                journalTokens.clear();
                set({ overviewData: emptyData(channel) });
            }
            const pending = [reloadRemoved(), reloadLineage()];
            if (get().overview.query.trim()) pending.push(reloadMatches());
            await Promise.all(pending);
        },
        dropOverviewData: () => {
            journalTokens.clear();
            ++removedToken; ++matchesToken; ++lineageToken;
            set({ overviewData: emptyData(null) });
        },
        showJournal: async (key) => {
            data({ journalShown: key });
            if (key && !get().overviewData.journals[key]) await fetchJournalPage(key, 'replace');
        },
        loadMoreJournal: async (key) => {
            const page = get().overviewData.journals[key];
            if (!page || page.loading || page.complete) return;
            await fetchJournalPage(key, 'older');
        },
        restoreRemoved: async (cellId, target) => {
            const channel = channelOf();
            if (!channel) return false;
            // Off the shelf at once; back where it was if the restore fails.
            const channelId=get().activeChannel?.id;
            const before = get().overviewData.removed;
            if (before) data({ removed: before.filter(entry => entry.cell_id !== cellId) });
            try {
                const res = await restoreRemovedCell(channel, cellId);
                // The route appends the column at the board's right end; a
                // drop elsewhere moves it there before either lands, so the
                // board never shows it at the end first.
                const moved = await placeRestored(channel, res.channel.main_stacks, res.stack_id, res.cell_id, target)
                    .catch((err: unknown) => {
                        data({ error: describeApiError(err) });
                        return null;
                    });
                const state = get();
                state.applyOptimisticChannel(res.channel);
                if (moved) state.applyOptimisticLayout(moved);
                const current = state.overview;
                view({
                    shelfSelection: current.shelfSelection === cellId ? null : current.shelfSelection,
                    picked: current.picked === cellId ? null : current.picked,
                });
                if(channelId&&get().activeChannel?.id===channelId)await get().hydrateCellDisplays([res.cell_id]);
                state.requestSelectCell(res.cell_id);
                void reloadRemoved();
                void reloadLineage();
                return true;
            } catch (err) {
                const now = get().overviewData.removed;
                const entry = before?.find(e => e.cell_id === cellId);
                if (now && entry && !now.some(e => e.cell_id === cellId)) {
                    const { sort, dir } = get().overview;
                    data({ removed: sortRemoved([...now, entry], sort, dir) });
                }
                data({ error: describeApiError(err) });
                return false;
            }
        },
        removeFromBoard: async (cellId, action) => {
            const channel = channelOf();
            const state = get();
            const stack = boardStackOf(state, cellId);
            const cell = stack?.cells.find(c => String(c.id) === cellId);
            if (!channel || !stack || !cell || !removableCard(state, cellId)) return false;
            if (cellHasLiveRun(state, cellId)) return false;
            const channelId=state.activeChannel!.id;
            try{await flushCellDisplays(channelId,[cellId]);}catch(error){data({error:describeApiError(error)});return false;}
            const order = boardOrder(state.activeChannel?.main_stacks ?? []);
            const at = order.indexOf(cellId);
            const next = order[at + 1] ?? order[at - 1] ?? null;
            // On the shelf at once, from what the board knows of it; the
            // shelf route's own card replaces it once the removal lands.
            const shelved = state.overviewData.removed;
            if (shelved && !shelved.some(entry => entry.cell_id === cellId)) {
                const { sort, dir } = state.overview;
                data({ removed: sortRemoved([...shelved, provisionalRemoved(cell, stack, new Date().toISOString(), action)], sort, dir) });
            }
            try {
                const body = removeBodyOf(action);
                const layout = await (body ? removeCellById(channel, cellId, body) : removeCellById(channel, cellId));
                get().dropCellDisplays(channelId,[cellId]);
                get().applyOptimisticLayout(layout);
                view({ shelfSelection: cellId, scrollTo: cellId });
                if (next) get().requestSelectCell(next);
                void reloadRemoved();
                void reloadLineage();
                return true;
            } catch (err) {
                const now = get().overviewData.removed;
                if (now && !shelved?.some(entry => entry.cell_id === cellId)) {
                    data({ removed: now.filter(entry => entry.cell_id !== cellId) });
                }
                data({ error: describeApiError(err) });
                return false;
            }
        },
        setRemovedTags: async (cellId, next, prior) => {
            const channel = channelOf();
            if (!channel) return false;
            const edit = normalizeTags(next);
            const entryTags = () => get().overviewData.removed?.find(entry => entry.cell_id === cellId)?.tags;
            const put = (tags: string[]) => {
                const now = get().overviewData.removed;
                if (now) data({ removed: now.map(entry => (entry.cell_id === cellId ? { ...entry, tags } : entry)) });
            };
            put(edit);
            try {
                const receipt = await putRemovedCellTags(channel, cellId, edit);
                if (entryTags() === edit) put(receipt.tags);
                return true;
            } catch (err) {
                if (entryTags() === edit) put(prior);
                data({ error: describeApiError(err) });
                return false;
            }
        },
        setOverviewDrop: (drop) => view({ drop }),
        refuseDrop: (reason, at) => view({ refusal: { reason, at, nonce: (get().overview.refusal?.nonce ?? 0) + 1 } }),
        overviewLineageChanged: (cellIds) => {
            const named = new Set(cellIds);
            invalidateJournals(key => key === ALL_CELLS || named.has(key));
            const loaded = get().overviewData;
            if (loaded.removed !== null) void reloadRemoved();
            if (loaded.removedMatches !== null) void reloadMatches();
            if (loaded.lineage !== null) void reloadLineage();
        },
        overviewRunChanged: (runId, cellId) => {
            invalidateJournals((key, page) => key === ALL_CELLS || key === cellId
                || page.events.some(event => (runId !== null && event.run?.run_id === runId)
                    || (cellId !== null && event.cells.some(ref => ref.cell_id === cellId))));
        },
        overviewResync: () => {
            const loaded = get().overviewData;
            if (!loaded.channel) return;
            invalidateJournals(() => true);
            if (loaded.removed !== null) void reloadRemoved();
            if (loaded.removedMatches !== null) void reloadMatches();
            if (loaded.lineage !== null) void reloadLineage();
        },
    };
};
