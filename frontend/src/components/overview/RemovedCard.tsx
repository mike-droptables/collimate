import { Clock, File, GitBranch, History, Minus } from 'lucide-react';
import type { CellActor, RemovedCell, RemovedSort } from '@/lib/api/cells';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { OVERVIEW_CARD_HEIGHT } from '@/stores/collimate/overviewSlice';
import { TagMenu } from '@/components/TagMenu';
import { JournalCardBody } from './JournalCardBody';
import { FileName, FileRows } from './journalGlyphs';
import { TagChips } from './TagChips';
import { when } from './journalText';

// A removed cell on the Removed shelf (the mock's `rmCard`): a ghost of an
// overview card, a dashed line-2 hairline on the canvas, never a fill. Its
// body says when it was made, last edited or last removed (the shelf's
// sort), by whom, its tags on one line, its first files and the head of its
// preview file, all from the shelf route's bounded payload. Its head ends
// in the Tags button (TagMenu, as on a board card: the edit goes to the
// removed record through the store's setRemovedTags), with Restore after it
// on the selected card. The anchor's card, while the journal is open, takes
// the journal style (JournalCardBody: its versions, files and runs) inside
// a solid teal edge, as the board's journal card.

/** Who made it: "you" for the person at the keyboard, else the runner. */
const actorName = (actor: CellActor): string =>
    actor.kind === 'user' ? 'you' : actor.runner_name ?? 'a runner';

const glyph = 'h-3 w-3 flex-none text-ink-3';

function Files({ files, total }: { files: string[]; total: number }) {
    // Three names, or two and "+n more".
    const shown = files.slice(0, total > 3 ? 2 : 3);
    return (
        <ul className="mt-[0.1875rem] list-none whitespace-nowrap pl-[1.125rem] leading-[1.6]">
            <FileRows paths={shown} among={files} />
            {total > shown.length && <li className="text-ink-3">+{total - shown.length} more</li>}
        </ul>
    );
}

/** The time each sort reads, and what the card calls it. */
function sortStamp(entry: RemovedCell, sort: RemovedSort): { label: string; at: string | null } {
    if (sort === 'created') return { label: 'Created', at: entry.created_at };
    if (sort === 'removed') return { label: 'Removed', at: entry.removed_at };
    return { label: 'Edited', at: entry.edited_at ?? entry.created_at };
}

/** The card at rest: the time the sort reads, the author, the tags, the
 *  first files and the head of the preview file. */
function RestBody({ entry, sort }: { entry: RemovedCell; sort: RemovedSort }) {
    const { label, at } = sortStamp(entry, sort);
    const preview = entry.preview;
    return (
        <>
            <div className="flex items-center gap-1.5 whitespace-nowrap text-ink-3">
                <Clock className={glyph} strokeWidth={1.6} />
                {label}{at ? ` ${when(at)}` : ''}
                <span className="ml-auto truncate">{actorName(entry.made_by)}</span>
            </div>
            <TagChips data-rm-tags tags={entry.tags} className="mt-1.5" />
            <div className="mt-2.5 flex items-center gap-1.5 text-[0.6875rem] font-semibold uppercase tracking-[0.08em] text-ink-2">
                <File className={glyph} strokeWidth={1.6} />Files ({entry.file_count})
            </div>
            <Files files={entry.files} total={entry.file_count} />
            {preview && (
                <>
                    <div className="mt-2.5 h-px bg-line" />
                    <div className="mt-[0.5625rem] flex min-w-0 items-center gap-1.5 whitespace-nowrap font-medium text-ink" data-rm-preview-name>
                        <File className={glyph} strokeWidth={1.6} />
                        <span className="truncate [&>span]:ml-1.5 [&>span]:font-normal"><FileName path={preview.path} /></span>
                    </div>
                    <pre className="mt-1.5 max-h-24 overflow-hidden whitespace-pre-wrap break-words rounded-md border border-line bg-code-bg px-[0.5625rem] py-[0.4375rem] font-mono text-[0.65625rem] leading-[1.5] text-ink-2">
                        {preview.text}
                    </pre>
                </>
            )}
        </>
    );
}

export interface RemovedCardProps {
    entry: RemovedCell;
    /** The shelf's sort: the card says "Created …", "Edited …" or "Removed …". */
    sort: RemovedSort;
    /** The shelf's selected card: a solid edge and Restore in its head. */
    selected: boolean;
    /** The anchor's card while the journal is open. */
    journalStyle: boolean;
    /** "Click again to follow its lines" (Only connected, picked once). */
    followHint: boolean;
    onClick: (event: React.MouseEvent) => void;
    onRestore: () => void;
    /** A restore is on its way: Restore is greyed. */
    restoring?: boolean;
}

/** One removed cell on the shelf. Its height is the shelf's `--rm-ch` (the
 *  journal style grows from it). */
export function RemovedCard({ entry, sort, selected, journalStyle, followHint, onClick, onRestore, restoring = false }: RemovedCardProps) {
    // The journal's card: a solid teal edge, over the selection's ink-3.
    const edge = journalStyle ? 'border-solid border-teal' : selected ? 'border-solid border-ink-3' : 'border-dashed border-line-2';
    const channel = useCollimateStore(s => s.overviewData.channel);
    return (
        <article
            data-rm-card={entry.cell_id}
            data-selected={selected || undefined}
            data-journal-style={journalStyle || undefined}
            aria-label={`Removed cell: ${entry.name}`}
            aria-current={selected || undefined}
            className={cn('relative flex cursor-pointer flex-col overflow-hidden rounded-lg border bg-canvas transition-[border-color] duration-[120ms] motion-reduce:transition-none', edge)}
            style={journalStyle
                ? { minHeight: `var(--rm-ch, ${OVERVIEW_CARD_HEIGHT}px)` }
                : { height: `var(--rm-ch, ${OVERVIEW_CARD_HEIGHT}px)` }}
            onClick={onClick}
        >
            <div className={cn('flex h-9 flex-none items-center gap-2.5 border-b pl-3.5 pr-3', selected ? 'border-solid border-line' : 'border-dashed border-line-2')}>
                <span className="inline-flex text-ink-3" aria-hidden><Minus className="h-3 w-3" strokeWidth={1.6} /></span>
                <h3 className="min-w-0 truncate text-[0.875rem] font-semibold tracking-[-0.005em] text-ink-2">{entry.name || 'Untitled cell'}</h3>
                <div className="ml-auto flex flex-none items-center gap-1.5 text-ink-3">
                    {channel && (
                        <TagMenu
                            channel={channel}
                            tags={entry.tags}
                            onSave={(next, prior) => {
                                void useCollimateStore.getState().setRemovedTags(entry.cell_id, next, prior);
                            }}
                        />
                    )}
                    {selected && (
                        <button
                            type="button"
                            data-rm-restore
                            disabled={restoring}
                            title="Put this cell back on the board, at the right end"
                            className="inline-flex h-[1.375rem] flex-none items-center gap-1.5 whitespace-nowrap rounded-sm border border-line-2 px-2 text-[0.75rem] text-ink-2 transition-colors hover:bg-sheet-2 hover:text-ink disabled:cursor-default disabled:opacity-45 disabled:hover:bg-transparent disabled:hover:text-ink-2"
                            onClick={(e) => { e.stopPropagation(); onRestore(); }}
                        >
                            <History className="h-3 w-3" strokeWidth={1.6} />
                            Restore
                        </button>
                    )}
                </div>
            </div>
            <div className="min-h-0 flex-1 overflow-hidden px-3.5 pb-3 pt-2.5 text-[0.75rem] text-ink-2">
                {journalStyle ? (
                    <JournalCardBody
                        cellId={entry.cell_id}
                        at={entry.edited_at ?? entry.created_at}
                        by={actorName(entry.made_by)}
                        files={entry.files}
                        fileCount={entry.file_count}
                    />
                ) : <RestBody entry={entry} sort={sort} />}
            </div>
            {followHint && (
                <div data-follow-hint className="absolute inset-x-0 bottom-0 flex h-[1.875rem] items-center gap-1.5 border-t border-line bg-canvas px-3.5 text-[0.75rem] text-ink-2">
                    <GitBranch className="h-3 w-3 text-ink-3" strokeWidth={1.6} />
                    Click again to follow its lines
                </div>
            )}
        </article>
    );
}
