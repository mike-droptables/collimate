import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { cn } from '@/lib/utils';
import { describeApiError } from '@/lib/api/client';
import { fetchChannelSize, type ManagedChannel } from '@/lib/api/storage';
import { formatBytes } from '@/lib/fileInfo';
import { btn, btnDanger, dialog, formInput, iconBtn } from '@/components/ui/formStyles';

/** What the confirmation says the delete frees: N from `GET …/size`
 *  (computed now when its cache is cold), unknown until it answers. */
type Freed = { state: 'reading' } | { state: 'known'; bytes: number } | { state: 'unknown' };

interface DeleteChannelDialogProps {
    channel: Pick<ManagedChannel, 'id' | 'name' | 'path'>;
    onCancel: () => void;
    /** Delete it (`DELETE /api/channels/{id}`); rejects with the refusal,
     *  which the dialog shows, keeping itself open. */
    onConfirm: () => Promise<void>;
}

/**
 * The channels page's delete confirmation (plan 1105 "The page"): the
 * type-the-name pattern, stating what goes and how much it frees at once:
 * "Deletes the channel and frees N (its folder <path>) immediately". The
 * size comes from `GET /api/channels/{id}/size`; while it is read (or when
 * it cannot be) the sentence names the folder alone. Delete is armed only
 * once the channel's name is typed. Delete lives on this page alone, never
 * inside a channel.
 *
 * P8 opens it from the Channels and Archived tabs' Delete… (ChannelTable's
 * `onDelete`), with `onConfirm` calling `deleteChannel` and refreshing.
 */
export function DeleteChannelDialog({ channel, onCancel, onConfirm }: DeleteChannelDialogProps) {
    const [freed, setFreed] = useState<Freed>({ state: 'reading' });
    const [typed, setTyped] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const inputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        let live = true;
        fetchChannelSize(channel.id).then(
            size => { if (live) setFreed({ state: 'known', bytes: size.bytes }); },
            () => { if (live) setFreed({ state: 'unknown' }); },
        );
        return () => { live = false; };
    }, [channel.id]);

    useEffect(() => { inputRef.current?.focus(); }, []);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onCancel(); };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [busy, onCancel]);

    const armed = typed.trim() === channel.name && !busy;
    const confirm = async () => {
        if (!armed) return;
        setBusy(true); setError('');
        try { await onConfirm(); }
        catch (e) { setError(describeApiError(e)); setBusy(false); }
    };

    const amount = freed.state === 'known' ? formatBytes(freed.bytes) : null;
    return createPortal(
        <div {...KEYBOARD_OWNER_PROPS}
            className="fixed inset-0 z-[460] flex items-center justify-center p-4 bg-(--lift) backdrop-blur-[0.25rem]"
            onClick={e => { if (e.target === e.currentTarget && !busy) onCancel(); }}>
            <div role="dialog" aria-label={`Delete ${channel.name}`} className={cn(dialog, 'w-full max-w-md')}>
                <div className="flex items-center justify-between h-9 px-4 border-b border-line">
                    <h2 className="text-[0.84375rem] font-semibold text-ink">Delete Channel</h2>
                    <button type="button" onClick={onCancel} disabled={busy} aria-label="Close" className={cn(iconBtn, 'h-[1.375rem] w-[1.375rem]')}>
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="p-4 space-y-3 [overflow-wrap:anywhere]">
                    <p data-delete-frees className="text-[0.78125rem] text-ink-2 leading-relaxed">
                        {amount
                            ? <>Deletes the channel and frees {amount} (its folder <span className="font-mono text-ink">{channel.path}</span>) immediately.</>
                            : <>Deletes the channel and its folder <span className="font-mono text-ink">{channel.path}</span> immediately{freed.state === 'reading' ? ' (reading its size…)' : ''}.</>}
                        {' '}Its full history and the git history of its repositories go with it. This cannot be undone.
                    </p>
                    <label className="block space-y-1.5">
                        <span className="block text-[0.78125rem] text-ink-2">
                            Type <span className="text-red font-mono">{channel.name}</span> to confirm:
                        </span>
                        <input ref={inputRef} type="text" value={typed} disabled={busy} placeholder={channel.name}
                            onChange={e => setTyped(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter') void confirm(); }}
                            className={formInput} />
                    </label>
                    {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
                </div>
                <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
                    <button type="button" className={btn} disabled={busy} onClick={onCancel}>Cancel</button>
                    <button type="button" className={btnDanger} disabled={!armed} onClick={() => void confirm()}>
                        {busy ? 'Deleting…' : 'Delete'}
                    </button>
                </div>
            </div>
        </div>,
        document.body,
    );
}
