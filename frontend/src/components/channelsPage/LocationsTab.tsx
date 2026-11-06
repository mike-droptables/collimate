import { useState } from 'react';
import { cn } from '@/lib/utils';
import { describeApiError } from '@/lib/api/client';
import {
    removeLocation, setDefaultLocation,
    type AddedLocation, type ClaimedLocation, type LocationState, type ManagedChannel, type StorageLocation,
} from '@/lib/api/storage';
import { localFolderPicker } from '@/lib/desktop';
import { formatBytes } from '@/lib/fileInfo';
import type { LocationListing } from '@/hooks/useStorage';
import { btn, btnDanger, btnPrimary, chip } from '@/components/ui/formStyles';
import { AddLocationDialog, ClaimLocationDialog } from './LocationDialogs';

const STATE_LABEL: Record<LocationState, string> = {
    ok: 'Ready',
    missing: 'Missing',
    unwritable: 'Not writable',
    foreign: 'Owned by another installation',
};

const STATE_TONE: Record<LocationState, string> = {
    ok: 'text-ink-3',
    missing: 'text-red',
    unwritable: 'text-red',
    foreign: 'text-brass',
};

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? '' : 's'}`;

/** A location's size: its channels' sizes as the manage listing last had
 *  them (the locations route sizes nothing). Null while none is sized. */
export function locationBytes(locationId: string, channels: readonly ManagedChannel[] | null): number | null {
    const sized = (channels ?? []).filter(row => row.location_id === locationId && row.bytes != null);
    return sized.length ? sized.reduce((sum, row) => sum + (row.bytes ?? 0), 0) : null;
}

export interface LocationsTabProps {
    locations: LocationListing;
    /** The manage listing, for each location's size. */
    channels: ManagedChannel[] | null;
    /** Read the listing and the locations again, then the app's state (a
     *  claim adopts channels into the window's list). */
    onChanged: () => Promise<void>;
}

/**
 * The Locations tab (plan 1105 "The page", P8): the window's server's
 * channel locations: path with the Default badge, state (or warning),
 * channel count, size and free / total. Add location… (the folder chooser on
 * the desktop for the local server, else a typed path, checked as typed),
 * Set default (exactly one is; no channel moves), Remove (an empty
 * location that is not the default, or a foreign or missing entry) and
 * Claim… (a foreign location's only action). Never asked at creation: a new
 * channel goes to the default.
 */
export function LocationsTab({ locations, channels, onChanged }: LocationsTabProps) {
    const [adding, setAdding] = useState(false);
    const [claiming, setClaiming] = useState<StorageLocation | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState('');
    const [note, setNote] = useState('');
    const pickFolder = localFolderPicker();

    const act = async (row: StorageLocation, op: () => Promise<unknown>, done: string) => {
        setBusy(row.id); setError(''); setNote('');
        try {
            await op();
            await onChanged();
            setNote(done);
        } catch (e) {
            setError(describeApiError(e));
        } finally {
            setBusy(null);
        }
    };

    const adopted = (count: number, skipped: number) => [
        count ? `adopted ${plural(count, 'channel')}` : '',
        skipped ? `skipped ${plural(skipped, 'channel')} this server already has` : '',
    ].filter(Boolean).join(', ');

    const added = async (result: AddedLocation) => {
        setAdding(false);
        await onChanged();
        const brought = result.claimed ? adopted(result.claimed.adopted.length, result.claimed.skipped.length) : '';
        setNote(`Added ${result.location.path}${result.location.is_default ? ' as the default' : ''}${brought ? `; ${brought}` : ''}.`);
    };
    const claimed = async (result: ClaimedLocation) => {
        setClaiming(null);
        await onChanged();
        const brought = adopted(result.adopted.length, result.skipped.length);
        setNote(`Claimed ${result.location.path}${brought ? `; ${brought}` : ''}.`);
    };

    const list = locations.list;
    return <>
        <div className="flex flex-wrap items-center gap-2">
            <p className="min-w-0 flex-1 text-[0.78125rem] text-ink-3">
                Folders on this server that hold channel folders. New channels go to the default location; move a channel from the Channels tab.
            </p>
            <button type="button" className={btnPrimary} onClick={() => { setAdding(true); setNote(''); setError(''); }}>Add location…</button>
        </div>
        {locations.error && <p role="alert" className="text-[0.75rem] text-red">Could not read this server&rsquo;s locations: {locations.error}</p>}
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        {note && <p role="status" className="text-[0.75rem] text-ink-2">{note}</p>}
        {list === null && !locations.error && <p className="text-[0.78125rem] text-ink-3">Reading this server&rsquo;s locations…</p>}
        {list && <div className="overflow-x-auto">
            <table aria-label="Locations" className="w-full border-collapse text-left text-[0.78125rem] text-ink-2">
                <thead>
                    <tr className="border-b border-line text-[0.71875rem] text-ink-3">
                        <th scope="col" className="py-1.5 pr-3 font-medium">Path</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium text-right">Channels</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium text-right">Size</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium text-right">Free / total</th>
                        <th scope="col" className="py-1.5 pr-3 font-medium">State</th>
                        <th scope="col" className="py-1.5 font-medium"><span className="sr-only">Actions</span></th>
                    </tr>
                </thead>
                <tbody>
                    {list.map(row => {
                        const foreign = row.state === 'foreign';
                        const bytes = foreign ? null : locationBytes(row.id, channels);
                        const waiting = busy !== null;
                        return <tr key={row.id} data-location-row={row.id} data-location-state={row.state} className="border-b border-line align-top">
                            <th scope="row" className="py-2 pr-3 font-normal">
                                <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                                    <span className="break-all font-mono text-[0.75rem] text-ink">{row.path}</span>
                                    {row.is_default && <span className={cn(chip, 'text-teal')}>Default</span>}
                                </span>
                            </th>
                            <td className="py-2 pr-3 text-right">{row.channel_count}</td>
                            <td className="py-2 pr-3 text-right whitespace-nowrap" title={foreign ? 'Not this server’s to size' : 'Its channels, as last measured'}>
                                {bytes != null ? formatBytes(bytes) : '—'}
                            </td>
                            <td className="py-2 pr-3 text-right whitespace-nowrap">
                                {row.free_bytes != null && row.total_bytes != null ? `${formatBytes(row.free_bytes)} / ${formatBytes(row.total_bytes)}` : '—'}
                            </td>
                            <td className={cn('py-2 pr-3', STATE_TONE[row.state])}>
                                {STATE_LABEL[row.state]}
                                {foreign && <span className="block text-[0.71875rem]">
                                    (<span className="font-mono">{row.owner_data_dir ?? 'unknown data dir'}</span>)
                                </span>}
                                {row.warning && <span data-location-warning={row.warning.code} className="block text-[0.71875rem] text-brass">{row.warning.message}</span>}
                            </td>
                            <td className="py-1.5">
                                <div className="flex flex-wrap justify-end gap-1.5">
                                    {row.actions.includes('claim') && <button type="button" className={btn} disabled={waiting}
                                        aria-label={`Claim ${row.path}`} onClick={() => setClaiming(row)}>Claim…</button>}
                                    {row.actions.includes('set_default') && <button type="button" className={btn} disabled={waiting}
                                        aria-label={`Make ${row.path} the default`}
                                        onClick={() => void act(row, () => setDefaultLocation(row.id), `New channels now go to ${row.path}.`)}>
                                        {busy === row.id ? 'Setting…' : 'Make default'}
                                    </button>}
                                    {row.actions.includes('remove') && <button type="button" className={btnDanger} disabled={waiting}
                                        aria-label={`Remove ${row.path}`}
                                        title={foreign || row.state === 'missing' ? 'Drops it from the list' : 'Removes the location (its folder too, when empty)'}
                                        onClick={() => void act(row, () => removeLocation(row.id), `Removed ${row.path}.`)}>
                                        {busy === row.id ? 'Removing…' : 'Remove'}
                                    </button>}
                                </div>
                            </td>
                        </tr>;
                    })}
                </tbody>
            </table>
        </div>}
        {adding && <AddLocationDialog pickFolder={pickFolder} onClose={() => setAdding(false)} onAdded={added} />}
        {claiming && <ClaimLocationDialog location={claiming} onClose={() => setClaiming(null)} onClaimed={claimed} />}
    </>;
}
