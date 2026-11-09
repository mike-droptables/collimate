import { useState } from 'react';
import { cn } from '@/lib/utils';
import { describeApiError } from '@/lib/api/client';
import { moveEnded, refusalCode, type ManagedChannel, type MoveJob, type MoveJobState, type StorageLocation } from '@/lib/api/storage';
import { formatBytes } from '@/lib/fileInfo';
import type { MoveProgress } from '@/hooks/useStorage';
import { btn, btnPrimary } from '@/components/ui/formStyles';
import { PageDialog } from './PageDialog';

export const MOVE_STATE_LABEL: Record<MoveJobState, string> = {
    copying: 'Copying',
    verifying: 'Verifying the copy',
    switching: 'Switching over',
    cleaning: 'Removing the old folder',
    done: 'Moved',
    failed: 'Failed',
};

/** What the move's percentage is: bytes copied of the folder's size (a
 *  rename on one filesystem has none to copy, so it reads its state). */
export function movePercent(job: Pick<MoveJob, 'state' | 'bytes_done' | 'bytes_total'>): number {
    if (job.state === 'done') return 100;
    if (job.bytes_total <= 0) return 0;
    return Math.min(100, Math.floor((job.bytes_done / job.bytes_total) * 100));
}

/** Why a location cannot take the channel, or null when it can. */
function unusable(location: StorageLocation, channel: Pick<ManagedChannel, 'location_id'>): string | null {
    if (location.id === channel.location_id) return 'The channel is here';
    if (location.state === 'foreign') return 'Owned by another installation';
    if (location.state === 'missing') return 'The folder is missing';
    if (location.state === 'unwritable') return 'Not writable';
    return null;
}

/** A failed job keeps the channel where it was (plan S7): the copy is
 *  removed, the source untouched. */
const KEPT = 'The channel stays where it was.';

interface MoveChannelDialogProps {
    channel: Pick<ManagedChannel, 'id' | 'name' | 'location_id'>;
    locations: StorageLocation[];
    /** The page's move (useStorage), shown when it is this channel's. */
    move: MoveProgress | null;
    /** Start it; rejects with the server's refusal. */
    onStart: (locationId: string) => Promise<void>;
    onClose: () => void;
}

/**
 * Move… (plan "The page", Channels and Archived): choose another owned
 * location, then the job's progress (state, bytes copied of the total,
 * files) polled every 500 ms by useStorage. Closing the dialog does not
 * stop the move: the channel's row shows it until it ends. A refusal shows
 * here with the server's sentence; a failed job says the channel stayed.
 * An archived channel moves like any other.
 */
export function MoveChannelDialog({ channel, locations, move, onStart, onClose }: MoveChannelDialogProps) {
    const candidates = locations.filter(location => location.state !== 'foreign' || location.id === channel.location_id);
    const first = candidates.find(location => !unusable(location, channel));
    const [target, setTarget] = useState<string | null>(first?.id ?? null);
    const [starting, setStarting] = useState(false);
    const [error, setError] = useState<{ code: string | null; message: string } | null>(null);

    const mine = move && move.channelId === channel.id ? move : null;
    const job = mine?.job ?? null;
    const ended = !!job && moveEnded(job);
    const otherMove = move && move.channelId !== channel.id && !(move.job && moveEnded(move.job)) && !move.error ? move : null;
    const destination = (id: string | undefined) => locations.find(location => location.id === id)?.path ?? id ?? '';

    const start = async () => {
        if (!target) return;
        setStarting(true); setError(null);
        try {
            await onStart(target);
        } catch (e) {
            setError({ code: refusalCode(e), message: describeApiError(e) });
        } finally {
            setStarting(false);
        }
    };

    const choosing = !mine;
    return <PageDialog label={`Move ${channel.name}`} title="Move Channel" onClose={onClose} busy={starting}
        footer={choosing
            ? <>
                <button type="button" className={btn} disabled={starting} onClick={onClose}>Cancel</button>
                <button type="button" className={btnPrimary} disabled={!target || starting || !!otherMove} onClick={() => void start()}>
                    {starting ? 'Starting…' : 'Move'}
                </button>
            </>
            : <button type="button" className={btn} onClick={onClose}>{ended || mine?.error ? 'Close' : 'Hide'}</button>}>
        {choosing && <>
            <p>Move <span className="font-medium text-ink">{channel.name}</span> to another location. Its folder is copied, checked and then switched over; until then it opens and reads as usual, and changes wait.</p>
            {candidates.length === 0 && <p className="text-ink-3">This server has no other location. Add one on the Locations tab.</p>}
            <fieldset className="space-y-1.5" aria-label="Destination">
                {candidates.map(location => {
                    const why = unusable(location, channel);
                    return <label key={location.id} className={cn('flex items-start gap-2', why && 'opacity-55')}>
                        <input type="radio" name="move-destination" className="mt-1" value={location.id} disabled={!!why || starting}
                            checked={target === location.id} onChange={() => setTarget(location.id)} />
                        <span className="min-w-0">
                            <span className="block break-all font-mono text-ink">{location.path}</span>
                            <span className="block text-[0.71875rem] text-ink-3">
                                {why ?? (location.free_bytes != null ? `${formatBytes(location.free_bytes)} free` : 'Free space unknown')}
                                {location.is_default ? ' · Default' : ''}
                            </span>
                        </span>
                    </label>;
                })}
            </fieldset>
            {otherMove && <p className="text-brass">{otherMove.channelName} is moving; this server moves one channel at a time.</p>}
            {error && <p role="alert" data-move-refusal={error.code ?? ''} className="text-red">{error.message}</p>}
        </>}
        {mine && <div data-move-progress={job?.state ?? 'starting'} className="space-y-2">
            <p>Moving <span className="font-medium text-ink">{channel.name}</span> to <span className="break-all font-mono text-ink">{destination(mine.toLocationId)}</span>.</p>
            <div role="progressbar" aria-label="Move progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={job ? movePercent(job) : 0}
                className="h-1.5 w-full overflow-hidden rounded-full bg-sheet-3">
                <div className={cn('h-full rounded-full transition-[width]', job?.state === 'failed' ? 'bg-red' : 'bg-teal')}
                    style={{ width: `${job ? movePercent(job) : 0}%` }} />
            </div>
            <p className="text-ink-3">
                {job
                    ? <>{MOVE_STATE_LABEL[job.state]}{job.method === 'rename' ? ' (one filesystem: a rename)' : ''}
                        {job.bytes_total > 0 && <> · {formatBytes(job.bytes_done)} of {formatBytes(job.bytes_total)}</>}
                        {job.files_total > 0 && <> · {job.files_done} of {job.files_total} files</>}</>
                    : 'Starting…'}
            </p>
            {job?.state === 'done' && <p role="status" className="text-ink-2">Moved to <span className="break-all font-mono">{destination(job.to_location_id)}</span>.</p>}
            {job?.warning && <p className="text-brass">{job.warning}</p>}
            {job?.state === 'failed' && <p role="alert" data-move-error={job.error?.code ?? ''} className="text-red">
                {job.error?.message ?? 'The move failed.'} {KEPT}
            </p>}
            {mine.error && <p role="alert" className="text-red">The move&rsquo;s progress could not be read: {mine.error}</p>}
        </div>}
    </PageDialog>;
}
