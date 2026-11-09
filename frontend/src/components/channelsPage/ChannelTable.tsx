import { cn } from '@/lib/utils';
import { moveEnded, type ManagedChannel, type ManagedChannelState, type MoveJob } from '@/lib/api/storage';
import { formatBytes } from '@/lib/fileInfo';
import { btn, btnDanger, chip } from '@/components/ui/formStyles';

/** A row action: absent when the table does not offer it. */
type RowAction = (row: ManagedChannel) => void;

export interface ChannelTableProps {
    /** The table's accessible name ("Channels", "Archived channels"). */
    label: string;
    rows: ManagedChannel[];
    /** The window's server's name, on every row: the page shows one server. */
    serverName: string;
    /** The row whose action is under way (its id), or null. Every action
     *  waits while one runs. */
    busy: string | null;
    /** What the table says with no rows. */
    empty: string;
    /** The location's own path by `location_id` (the Locations tab's);
     *  absent or unknown: the folder's parent. */
    locationPath?: (locationId: string) => string | undefined;
    /** The page's move job, shown on its channel's row while it runs. */
    moving?: { channelId: string; job: MoveJob | null } | null;
    /** Open the channel in this window (an archived one opens read-only). */
    onOpen: RowAction;
    /** "Open read-only" on the Archived tab, "Open" elsewhere. */
    openLabel?: string;
    onRename?: RowAction;
    onArchive?: RowAction;
    onUnarchive?: RowAction;
    /** Move… (asks for a location, then follows the job). */
    onMove?: RowAction;
    /** Delete… (the type-the-name confirmation with the size it frees). */
    onDelete?: RowAction;
}

const STATE_LABEL: Record<ManagedChannelState, string> = {
    ok: 'Ready',
    moving: 'Moving',
    unavailable: 'Unavailable',
};

const STATE_TONE: Record<ManagedChannelState, string> = {
    ok: 'text-ink-3',
    moving: 'text-brass',
    unavailable: 'text-red',
};

/** The folder's parent: the location when the locations are not read. */
function folderLocation(path: string): string {
    const trimmed = path.replace(/[\\/]+$/, '');
    const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
    return cut > 0 ? trimmed.slice(0, cut) : trimmed;
}

/**
 * One table of a server's channels: the Channels tab's (every channel that
 * is not archived) and the Archived tab's. Columns: name, server, location,
 * size and state; each row's actions after them. A row the server cannot
 * use (moving, unavailable) does not open, and says why; the row of the
 * page's running move shows its progress.
 */
export function ChannelTable({
    label, rows, serverName, busy, empty, locationPath, moving, onOpen, openLabel = 'Open',
    onRename, onArchive, onUnarchive, onMove, onDelete,
}: ChannelTableProps) {
    if (rows.length === 0) return <p className="py-2 text-[0.78125rem] text-ink-3">{empty}</p>;
    const waiting = busy !== null;
    const running = moving && !(moving.job && moveEnded(moving.job)) ? moving : null;
    return (
        <div className="overflow-x-auto">
            <table aria-label={label} className="w-full border-collapse text-left text-[0.78125rem] text-ink-2">
                <thead>
                    <tr className="border-b border-line text-[0.71875rem] text-ink-3">
                        <th scope="col" className="py-1.5 pr-3 font-medium">Name</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium">Server</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium">Location</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium text-right">Size</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium">State</th>
                        <th scope="col" className="py-1.5 font-medium"><span className="sr-only">Actions</span></th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map(row => {
                        const job = running?.channelId === row.id ? running.job : null;
                        const isMoving = running?.channelId === row.id;
                        const state: ManagedChannelState = isMoving ? 'moving' : row.state;
                        const usable = state === 'ok';
                        const location = locationPath?.(row.location_id) ?? folderLocation(row.path);
                        const percent = job && job.bytes_total > 0 ? Math.floor((job.bytes_done / job.bytes_total) * 100) : null;
                        return (
                            <tr key={row.id} data-channel-row={row.id} data-channel-state={state} className="border-b border-line align-top">
                                <th scope="row" className="py-2 pr-3 font-normal">
                                    <span className="flex min-w-0 items-center gap-1.5">
                                        <span className="truncate font-medium text-ink">{row.name}</span>
                                        {row.active && <span className={cn(chip, 'text-brass')}>Open</span>}
                                    </span>
                                </th>
                                <td className="py-2 pr-3 whitespace-nowrap">{serverName}</td>
                                <td className="py-2 pr-3 max-w-[14rem] truncate font-mono text-[0.71875rem] text-ink-3" title={row.path}>{location}</td>
                                <td className="py-2 pr-3 text-right whitespace-nowrap" title={row.computed_at ? `As of ${row.computed_at}` : 'Not sized yet'}>
                                    {row.bytes != null ? formatBytes(row.bytes) : '—'}
                                </td>
                                <td className={cn('py-2 pr-3 whitespace-nowrap', STATE_TONE[state])} title={row.reason ?? undefined}>
                                    {STATE_LABEL[state]}{percent !== null && <> {percent}%</>}
                                    {row.reason && !isMoving && <span className="block whitespace-normal text-[0.71875rem]">{row.reason}</span>}
                                </td>
                                <td className="py-1.5">
                                    <div className="flex flex-wrap justify-end gap-1.5">
                                        <button type="button" className={btn} disabled={waiting || !usable}
                                            title={usable ? undefined : `${row.name} cannot open while it is ${STATE_LABEL[state].toLowerCase()}`}
                                            aria-label={`${openLabel} ${row.name}`}
                                            onClick={() => onOpen(row)}>{openLabel}</button>
                                        {onRename && <button type="button" className={btn} disabled={waiting || isMoving}
                                            aria-label={`Rename ${row.name}`} onClick={() => onRename(row)}>Rename…</button>}
                                        {onArchive && <button type="button" className={btn} disabled={waiting || !usable}
                                            aria-label={`Archive ${row.name}`} onClick={() => onArchive(row)}>{busy === row.id ? 'Archiving…' : 'Archive'}</button>}
                                        {onUnarchive && <button type="button" className={btn} disabled={waiting || !usable}
                                            aria-label={`Unarchive ${row.name}`} onClick={() => onUnarchive(row)}>{busy === row.id ? 'Unarchiving…' : 'Unarchive'}</button>}
                                        {onMove && <button type="button" className={btn} disabled={waiting || (!usable && !isMoving)}
                                            title={isMoving ? 'Show the move' : undefined}
                                            aria-label={`Move ${row.name}`} onClick={() => onMove(row)}>{isMoving ? 'Moving…' : 'Move…'}</button>}
                                        {onDelete && <button type="button" className={btnDanger} disabled={waiting || isMoving || row.state === 'moving'}
                                            aria-label={`Delete ${row.name}`} onClick={() => onDelete(row)}>Delete…</button>}
                                    </div>
                                </td>
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}
