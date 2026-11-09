import { useEffect, useId, useRef, useState } from 'react';
import { describeApiError } from '@/lib/api/client';
import {
    addLocation, checkLocation, claimLocation, refusalDetail,
    type AddedLocation, type ClaimedLocation, type LocationCheck, type LocationOwner, type StorageLocation,
} from '@/lib/api/storage';
import type { PickFolderOptions } from '@/lib/desktop';
import { btn, btnPrimary, formInput } from '@/components/ui/formStyles';
import { PageDialog } from './PageDialog';

/** How long typing rests before the path is checked. */
export const CHECK_DELAY_MS = 350;

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? '' : 's'}`;

/** What a claim says it does (plan "The page", Locations). */
function ClaimStatement({ owner, path }: { owner: Pick<LocationOwner, 'data_dir' | 'channel_count'>; path: string }) {
    return <p data-claim-statement>
        The folder <span className="font-mono text-ink">{path}</span> belongs to the Collimate installation at{' '}
        <span className="font-mono text-ink">{owner.data_dir ?? 'an unknown data dir'}</span> ({plural(owner.channel_count, 'channel')}).
        Claiming makes this server its owner and adopts those channels; the other installation will no longer open them.
    </p>;
}

/** The owner's state under a claim: running (the claim is off), unknown
 *  (the explicit confirmation, D18), or checked and stopped (nothing). */
function OwnerState({ running, dataDir, confirmed, onConfirmed }: {
    running: boolean | null; dataDir: string | null; confirmed: boolean; onConfirmed: (next: boolean) => void;
}) {
    if (running === true) {
        return <p role="alert" data-owner-running className="text-red">
            That installation&rsquo;s server is running (data dir <span className="font-mono">{dataDir ?? 'unknown'}</span>). Stop it, then claim.
        </p>;
    }
    if (running === null) {
        return <label data-owner-unchecked className="flex items-start gap-2">
            <input type="checkbox" className="mt-0.5" checked={confirmed} onChange={e => onConfirmed(e.target.checked)} />
            <span>
                Its owner cannot be checked (the data dir <span className="font-mono">{dataDir ?? 'unknown'}</span> is gone or on another
                machine). Claim it anyway: if that installation still runs, it will no longer open these channels.
            </span>
        </label>;
    }
    return null;
}

/** A refusal's owner state when it is `owner_running` or `owner_unchecked`. */
function ownerRefusal(error: unknown): { running: boolean | null; dataDir: string | null; message: string } | null {
    const detail = refusalDetail(error);
    if (!detail || (detail.code !== 'owner_running' && detail.code !== 'owner_unchecked')) return null;
    const dataDir = typeof detail.owner_data_dir === 'string' ? detail.owner_data_dir : null;
    return { running: detail.code === 'owner_running' ? true : null, dataDir, message: detail.message };
}

interface AddLocationDialogProps {
    /** The desktop's folder chooser, only for the local server
     *  (`localFolderPicker()`); absent: the path is typed. */
    pickFolder?: (options: PickFolderOptions) => Promise<string | null>;
    onClose: () => void;
    /** The location was added (and claimed); the page reads the lists again. */
    onAdded: (added: AddedLocation) => Promise<void> | void;
}

/**
 * Add location… (plan "The page", Locations): a server path, typed or (on
 * the desktop, for the local server) chosen, checked as it is typed by
 * `POST /api/storage/locations/check`. A folder another installation owns
 * shows its owner and channel count and offers Claim and add, off while the
 * owner's server runs, behind the explicit confirmation when the owner
 * cannot be checked. "Make it the default" is the one other choice; a new
 * channel never asks where it goes.
 */
export function AddLocationDialog({ pickFolder, onClose, onAdded }: AddLocationDialogProps) {
    const [path, setPath] = useState('');
    const [check, setCheck] = useState<LocationCheck | null>(null);
    const [checking, setChecking] = useState(false);
    const [makeDefault, setMakeDefault] = useState(false);
    const [confirmed, setConfirmed] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const seq = useRef(0);
    const inputRef = useRef<HTMLInputElement>(null);
    const fieldId = useId();

    useEffect(() => { inputRef.current?.focus(); }, []);

    useEffect(() => {
        const typed = path.trim();
        const mine = ++seq.current;
        setError('');
        setConfirmed(false);
        if (!typed) { setCheck(null); setChecking(false); return; }
        setChecking(true);
        const timer = setTimeout(() => {
            checkLocation(typed).then(
                answer => { if (mine === seq.current) { setCheck(answer); setChecking(false); } },
                e => { if (mine === seq.current) { setCheck(null); setChecking(false); setError(describeApiError(e)); } },
            );
        }, CHECK_DELAY_MS);
        return () => clearTimeout(timer);
    }, [path]);

    const choose = async () => {
        if (!pickFolder) return;
        try {
            const chosen = await pickFolder({ title: 'Choose a folder for channels', defaultPath: path.trim() || undefined });
            if (chosen) setPath(chosen);
        } catch (e) {
            setError(describeApiError(e));
        }
    };

    const owner = check?.error?.code === 'location_owned' ? check.owned_by ?? null : null;
    const claimable = !!owner && owner.owner_running !== true && (owner.owner_running !== null || confirmed);
    // `checking` covers every keystroke until its answer lands, so `check`
    // is always the typed path's here.
    const ready = !checking && !busy && !!check && (check.ok || claimable);

    const submit = async () => {
        if (!ready || !check) return;
        setBusy(true); setError('');
        try {
            const added = await addLocation({
                path: path.trim(), default: makeDefault,
                ...(owner ? { claim: true, confirm_unchecked: owner.owner_running === null && confirmed } : {}),
            });
            await onAdded(added);
        } catch (e) {
            const refused = ownerRefusal(e);
            if (refused && check.owned_by) {
                // The owner's state moved since the check: show it as it is now.
                setCheck({ ...check, owned_by: { ...check.owned_by, owner_running: refused.running, data_dir: refused.dataDir ?? check.owned_by.data_dir } });
                setConfirmed(false);
            } else {
                setError(describeApiError(e));
            }
            setBusy(false);
        }
    };

    return <PageDialog label="Add location" title="Add Location" onClose={onClose} busy={busy}
        footer={<>
            <button type="button" className={btn} disabled={busy} onClick={onClose}>Cancel</button>
            <button type="button" className={btnPrimary} disabled={!ready} onClick={() => void submit()}>
                {busy ? (owner ? 'Claiming…' : 'Adding…') : owner ? 'Claim and add' : 'Add location'}
            </button>
        </>}>
        <p>A location is a folder on the server that holds channel folders. New channels go to the default location.</p>
        <div className="space-y-1.5">
            <label htmlFor={fieldId} className="block text-[0.75rem] text-ink-2">Folder on the server</label>
            <div className="flex gap-2">
                <input ref={inputRef} id={fieldId} type="text" value={path} disabled={busy} spellCheck={false}
                    placeholder="/absolute/path/to/a/folder" className={`${formInput} font-mono`}
                    onChange={e => setPath(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void submit(); }} />
                {pickFolder && <button type="button" className={btn} disabled={busy} onClick={() => void choose()}>Choose…</button>}
            </div>
        </div>
        <div aria-live="polite" data-location-check className="min-h-[1.25rem]">
            {checking && <p className="text-ink-3">Checking…</p>}
            {!checking && check && check.ok && <p className="text-ink-2">
                {check.creates ? 'Creates the folder ' : 'Adds the folder '}<span className="font-mono text-ink">{check.path}</span>.
            </p>}
            {!checking && check?.error && !owner && <p role="alert" data-check-error={check.error.code} className="text-red">{check.error.message}</p>}
            {!checking && check?.warning && <p data-check-warning={check.warning.code} className="text-brass">{check.warning.message}</p>}
        </div>
        {!checking && owner && check && <>
            <ClaimStatement owner={owner} path={check.path} />
            <OwnerState running={owner.owner_running} dataDir={owner.data_dir} confirmed={confirmed} onConfirmed={setConfirmed} />
        </>}
        <label className="flex items-center gap-2">
            <input type="checkbox" checked={makeDefault} disabled={busy} onChange={e => setMakeDefault(e.target.checked)} />
            <span>Make it the default for new channels</span>
        </label>
        {error && <p role="alert" className="text-red">{error}</p>}
    </PageDialog>;
}

interface ClaimLocationDialogProps {
    location: Pick<StorageLocation, 'id' | 'path' | 'owner_data_dir' | 'channel_count'>;
    onClose: () => void;
    onClaimed: (claimed: ClaimedLocation) => Promise<void> | void;
}

/**
 * Claim… on a foreign location (one another installation claimed away):
 * the same statement as Claim and add. The listing does not say whether the
 * owner runs, so the first refusal does: `owner_running` turns the claim
 * off, `owner_unchecked` asks for the explicit confirmation and claims
 * again with it.
 */
export function ClaimLocationDialog({ location, onClose, onClaimed }: ClaimLocationDialogProps) {
    const [running, setRunning] = useState<boolean | null | undefined>(undefined);
    const [dataDir, setDataDir] = useState<string | null>(location.owner_data_dir ?? null);
    const [confirmed, setConfirmed] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const ready = !busy && running !== true && (running !== null || confirmed);
    const submit = async () => {
        if (!ready) return;
        setBusy(true); setError('');
        try {
            await onClaimed(await claimLocation(location.id, running === null && confirmed));
        } catch (e) {
            const refused = ownerRefusal(e);
            if (refused) {
                // OwnerState says it: the owner runs, or cannot be checked.
                setRunning(refused.running);
                if (refused.dataDir) setDataDir(refused.dataDir);
                setConfirmed(false);
            } else {
                setError(describeApiError(e));
            }
            setBusy(false);
        }
    };

    return <PageDialog label={`Claim ${location.path}`} title="Claim Location" onClose={onClose} busy={busy}
        footer={<>
            <button type="button" className={btn} disabled={busy} onClick={onClose}>Cancel</button>
            <button type="button" className={btnPrimary} disabled={!ready} onClick={() => void submit()}>{busy ? 'Claiming…' : 'Claim'}</button>
        </>}>
        <ClaimStatement owner={{ data_dir: dataDir, channel_count: location.channel_count }} path={location.path} />
        {running !== undefined && <OwnerState running={running} dataDir={dataDir} confirmed={confirmed} onConfirmed={setConfirmed} />}
        {error && <p role="alert" className="text-red">{error}</p>}
    </PageDialog>;
}
