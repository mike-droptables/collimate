import { useState } from 'react';
import { Archive } from 'lucide-react';
import { describeApiError } from '@/lib/api/client';
import { btn } from '@/components/ui/formStyles';

interface ArchivedBannerProps {
    /** Unarchive the open channel at once (D11): no confirmation, it only
     *  makes the channel writable again. Rejects with the server's refusal. */
    onUnarchive: () => Promise<void>;
    /** Open the channels page (on its Archived tab). */
    onManage: () => void;
}

/**
 * The strip under the top bar while the open channel is archived (plan
 * 1105, P7): "Archived · read-only." with Unarchive and Manage channels.
 * Every edit and run control is off meanwhile (ChannelReadOnlyContext);
 * unsaved drafts in this browser stay, and save once it is unarchived.
 */
export function ArchivedBanner({ onUnarchive, onManage }: ArchivedBannerProps) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const unarchive = async () => {
        setBusy(true); setError('');
        try { await onUnarchive(); }
        catch (e) { setError(describeApiError(e)); }
        finally { setBusy(false); }
    };
    return (
        <div role="region" aria-label="Archived channel" data-archived-banner
            className="flex flex-none flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 border-b border-line bg-sheet text-[0.78125rem] text-ink-2">
            <Archive aria-hidden className="size-3.5 flex-none text-brass" strokeWidth={1.6} />
            <span className="font-medium text-ink">Archived · read-only.</span>
            <span className="text-ink-3">Nothing here can change or run. Unsaved drafts stay, and save once it is unarchived.</span>
            <span className="ml-auto flex items-center gap-2">
                <button type="button" className={btn} disabled={busy} onClick={() => void unarchive()}>{busy ? 'Unarchiving…' : 'Unarchive'}</button>
                <button type="button" className={btn} onClick={onManage}>Manage channels</button>
            </span>
            {error && <p role="alert" className="basis-full text-[0.75rem] text-red">{error}</p>}
        </div>
    );
}
