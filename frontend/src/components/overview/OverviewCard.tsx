import React, { createContext, useContext } from 'react';
import { GitBranch, Minus } from 'lucide-react';
import type { Cell } from '@/lib/types';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { OVERVIEW_CARD_HEIGHT } from '@/stores/collimate/overviewSlice';
import { cellHasLiveRun, runsOnCell } from '@/lib/runningCell';
import { DynamicIcon } from '@/components/ui/DynamicIcon';
import type { CellActor, JournalEvent } from '@/lib/api/cells';
import { RunLight } from '@/components/RunLight';
import { OverviewCardPreview, cardFiles } from '@/components/OverviewCardPreview';
import { JournalCardBody } from './JournalCardBody';
import { TagChips } from './TagChips';
import { CellTagMenu } from '@/components/TagMenu';
import { currentChannelId } from '@/lib/currentChannel';
import { BuiltinChip } from '@/components/BuiltinChip';

// An overview card (the mock's cardHtml): a 273px sheet with the cell's
// 2.25rem head (its light, or a Capabilities cell's teal sheet and glyph,
// and its title) over the
// body at rest (OverviewCardPreview), or, for the cell whose journal is open,
// the journal-style body (JournalCardBody: its versions, its files and its
// runs; a version row opens the cell's History at it) inside a teal edge
// (cardEdge: teal over the selection's brass while its journal is open;
// the edge changes colour, never opacity). A card's head ends in its Tags
// button (TagMenu: a press on it neither drags nor selects the card, so
// Remove never appears under an open tag menu and moves its anchor; a
// built-in card has none); a selected standard card carries Remove after
// it (a Capabilities cell is never removed: nothing runs there, so it has
// no light either); with Only
// connected on, the selected card that a second click would follow says so
// in a foot. While the shelf's tag toggle is on (overview.showTags), the
// body opens with the cell's tags as chips (TagChips, two lines at most, so
// the card keeps its height). A double click is the board's (StackScene): it acts on the
// card its first click hit, wherever the second one lands.

/** What the board shows about the overview's state (null in the main view):
 *  the cells Only connected keeps, the cell drawn in journal style, and the
 *  card that says "Click again to follow its lines". */
export interface OverviewBoard {
    keep: ReadonlySet<string> | null;
    journalCellId: string | null;
    followCellId: string | null;
}
export const OverviewBoardContext = createContext<OverviewBoard | null>(null);
export const useOverviewBoard = () => useContext(OverviewBoardContext);

/** The small bordered head button (the mock's `.hbtn`): Remove here,
 *  Restore on a shelf card. */
const headButton = cn(
    'inline-flex h-[1.375rem] flex-none items-center gap-1.5 whitespace-nowrap rounded-sm border border-line-2 px-2',
    'text-[0.75rem] font-normal text-ink-2 hover:bg-sheet-2 hover:text-ink [&_svg]:h-3 [&_svg]:w-3',
    'disabled:cursor-default disabled:opacity-45 disabled:hover:bg-transparent disabled:hover:text-ink-2',
);

/** A card's edge: teal while the journal shows it (the open, on state,
 *  over the selection's brass, which returns when the journal closes or
 *  moves on), brass while selected, else the hairline. */
const cardEdge = (journal: boolean, selected: boolean): string =>
    journal ? 'border-teal' : selected ? 'border-brass' : 'border-line';

/** An actor as the overview names it. */
const actorName = (by: CellActor): string =>
    by.kind === 'runner' ? (by.runner_name || by.runner_id || 'runner') : 'you';

/** Is this event a version of `cellId` (made, edited or written by a run in it)? */
function versionEvent(event: JournalEvent, cellId: string): boolean {
    if (event.kind === 'make' || event.kind === 'edit' || event.kind === 'run') return event.cells[0]?.cell_id === cellId;
    return event.kind === 'run_cell' && event.cells[1]?.cell_id === cellId;
}

/** Who a card names: the runner running in the cell; else, in journal
 *  style, the author of its newest version the journal has loaded; else
 *  the cell's maker (the mock's meta `.by`): the runner whose run made it
 *  (the lineage's run_cell move into it), or you. */
function useCardAuthor(cellId: string, journal: boolean): string {
    return useCollimateStore(s => {
        const live = runsOnCell(s, cellId)[0];
        if (live) return `${live.runnerName || live.runnerId || live.generatorName} · running`;
        if (journal) {
            const last = s.overviewData.journals[cellId]?.events.find(e => versionEvent(e, cellId));
            if (last) return actorName(last.by);
        }
        const made = s.overviewData.lineage?.moves.find(m => m.op === 'run_cell' && m.to.some(t => t.cell_id === cellId));
        return made?.runner ? actorName({ kind: 'runner', ...made.runner }) : 'you';
    });
}

export interface OverviewCardProps {
    cell: Cell;
    /** The cell sits on the Capabilities stack. */
    capabilitiesCell?: boolean;
    /** The selected cell: the brass edge, and Remove in the head. */
    selected: boolean;
    /** Far outside the view: the body is the files count. */
    isOffscreen: boolean;
    /** Start a card drag (move into another column). Absent (a built-in
     *  card, or while Only connected filters the board): the card does not
     *  drag, and a drag on it pans the board. */
    onDragStart?: (e: React.PointerEvent) => void;
    /** Open the cell in the main view (a version row of the journal style). */
    onOpen?: () => void;
}

export const OverviewCard = React.memo(function OverviewCard({
    cell, capabilitiesCell = false, selected, isOffscreen, onDragStart, onOpen,
}: OverviewCardProps) {
    const board = useOverviewBoard();
    const journal = board?.journalCellId === cell.id;
    const follow = board?.followCellId === cell.id;
    const liveRun = useCollimateStore(s => cellHasLiveRun(s, cell.id));
    const author = useCardAuthor(cell.id, journal);
    const showTags = useCollimateStore(s => s.overview.showTags);

    return (
        <article
            // Only a card that drags blocks the board's pan (useStackCamera's
            // panBlockSelector); a press on one that does not pans.
            data-stack-card={onDragStart ? '' : undefined}
            data-cell-id={cell.id}
            data-ov-card={cell.id}
            data-journal={journal || undefined}
            aria-selected={selected}
            className={cn(
                'relative flex w-full flex-col overflow-hidden rounded-lg border bg-sheet shadow-(--shadow) select-none',
                'transition-[border-color] duration-[120ms] motion-reduce:transition-none',
                cardEdge(journal, selected),
                onDragStart ? 'cursor-grab' : 'cursor-pointer',
            )}
            style={journal ? { minHeight: OVERVIEW_CARD_HEIGHT } : { height: OVERVIEW_CARD_HEIGHT }}
            onPointerDown={(e) => {
                if (e.button !== 0 || !onDragStart || (e.target as Element).closest('button')) return;
                e.preventDefault(); // no native text selection while it drags
                onDragStart(e);
            }}
            onClick={(e) => {
                if ((e.target as Element).closest('button')) return;
                useCollimateStore.getState().selectCard(cell.id);
            }}
        >
            <div className={cn(
                'flex h-9 flex-none items-center gap-2.5 border-b border-line pl-[0.875rem] pr-3',
                capabilitiesCell && 'bg-teal-sheet',
            )}>
                {capabilitiesCell
                    ? <DynamicIcon name={cell.icon} className="h-3.5 w-3.5 flex-none text-teal" />
                    : <RunLight cellId={cell.id} />}
                <h3 className="m-0 min-w-0 truncate text-[0.875rem] font-semibold tracking-[-0.005em] text-ink" title={cell.name}>
                    {cell.name}
                </h3>
                {cell.builtin && <BuiltinChip cell={cell} />}
                {!cell.builtin && (
                    <div className="ml-auto flex flex-none items-center gap-1.5 text-ink-3">
                        <CellTagMenu channel={cell.channel || currentChannelId()} cell={cell} />
                        {selected && !capabilitiesCell && (
                            <button
                                type="button"
                                className={headButton}
                                disabled={liveRun}
                                title={liveRun ? 'Stop its runs to remove it' : 'Take it off the board, into Removed'}
                                onPointerDown={(e) => e.stopPropagation()}
                                onClick={(e) => {
                                    e.stopPropagation();
                                    void useCollimateStore.getState().removeFromBoard(cell.id);
                                }}
                            >
                                <Minus strokeWidth={1.6} />Remove
                            </button>
                        )}
                    </div>
                )}
            </div>
            <div className="min-h-0 flex-1 overflow-hidden px-[0.875rem] pb-3 pt-2.5 text-[0.75rem] text-ink-2">
                {showTags && <TagChips data-ov-tags tags={cell.tags ?? []} wrap className="mb-2" />}
                {journal && !isOffscreen ? (
                    <JournalCardBody
                        cellId={cell.id}
                        at={cell.created_at ?? null}
                        by={author}
                        files={cardFiles(cell).map(f => f.name)}
                        fileCount={cardFiles(cell).length}
                        onPickVersion={onOpen ? (nodeId, versionId) => {
                            const store = useCollimateStore.getState();
                            store.openCellHistory(cell.id);
                            store.setCellHistory(cell.id, { nodeId, versionId });
                            onOpen();
                        } : undefined}
                    />
                ) : <OverviewCardPreview cell={cell} isOffscreen={isOffscreen} author={author} />}
            </div>
            {follow && (
                <div className="absolute inset-x-0 bottom-0 flex h-[1.875rem] items-center gap-1.5 border-t border-line bg-sheet px-[0.875rem] text-[0.75rem] text-ink-2">
                    <GitBranch className="h-3 w-3 text-ink-3" strokeWidth={1.6} />Click again to follow its lines
                </div>
            )}
        </article>
    );
});
