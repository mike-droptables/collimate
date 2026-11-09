import { useState } from 'react';
import { Copy } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { copyToChannel, reviewCopy, type CopyReview, type LibraryResolution } from '@/lib/api/capabilityCells';
import { reportPackageSkips } from '@/lib/packageReports';
import { listedChannels } from '@/lib/channelArchived';
import { btn, btnPrimary, formLabel, formSelect } from '@/components/ui/formStyles';
import { PackageList, ReviewSection } from './PackageReview';
import { SubView } from './SubView';

const NO_CHANNELS: readonly { id: string; name: string; archived?: boolean }[] = [];

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** Where a library resolves on the target, in words. */
function resolution(row: LibraryResolution, target: string): string {
    switch (row.resolves) {
        case 'copy': return `comes with the copy, in ‘${row.cell}’`;
        case 'capabilities': return `already on ‘${target}’`;
        case 'app': return 'the app’s';
        default: return 'resolves nowhere';
    }
}

/**
 * Copy to channel, in place of the list: pick another channel, review what
 * lands there (the cell's packages with Add to's summary, each library its
 * cell.yaml names and where it resolves on the target, a library cell the
 * copy brings along, what admission holds back), then Copy. The copy
 * follows this cell: when its packages change here, the copy shows an
 * update.
 */
export function CellCopyView({ cellId, cellName, onBack, onDone }: {
    cellId: string; cellName: string; onBack: () => void; onDone: (notice: string) => void;
}) {
    const channelId = useCollimateStore(s => s.activeChannel?.id ?? '');
    // Every other channel that is not archived: an archived one takes no copy.
    const channels = listedChannels(useCollimateStore(s => s.state?.channels ?? NO_CHANNELS)).filter(channel => channel.id !== channelId);
    const [target, setTarget] = useState(channels[0]?.id ?? '');
    const [review, setReview] = useState<CopyReview | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const load = async (targetId: string) => {
        setTarget(targetId); setReview(null); setError('');
        if (!targetId) return;
        setBusy(true);
        try { setReview(await reviewCopy(channelId, cellId, targetId)); }
        catch (e) { setError(message(e, 'The copy could not be reviewed.')); }
        finally { setBusy(false); }
    };
    const copy = async () => {
        if (!review) return;
        setBusy(true); setError('');
        try {
            const result = await copyToChannel(channelId, cellId, review.target.channel_id, review.token);
            await reportPackageSkips(result.package_skips);
            onDone(`‘${cellName}’ is on ‘${review.target.channel}’’s Capabilities.`);
        } catch (e) { setError(message(e, 'The cell could not be copied.')); }
        finally { setBusy(false); }
    };

    return <SubView title={`Copy ‘${cellName}’ to channel`} onBack={onBack}>
        <div className="flex flex-col gap-3">
            {channels.length === 0
                ? <p className="text-[0.78125rem] text-ink-2">There is no other channel to copy to.</p>
                : <label className="flex flex-col gap-1"><span className={formLabel}>Channel</span>
                    <select aria-label="Channel" className={formSelect} value={target} disabled={busy}
                        onChange={e => void load(e.target.value)}>
                        {channels.map(channel => <option key={channel.id} value={channel.id}>{channel.name}</option>)}
                    </select>
                </label>}
            {!review && channels.length > 0 && <button type="button" className={`${btn} self-start`} disabled={busy || !target}
                onClick={() => void load(target)}>{busy ? 'Reviewing…' : 'Review the copy'}</button>}
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
            {review && <>
                <p className="text-[0.78125rem] text-ink-2">The copy keeps following this cell: when its packages change here, ‘{review.target.channel}’ shows an update.</p>
                {review.cells.map(cell => <ReviewSection key={cell.name} label={cell.brought ? `Brought along: ${cell.name}` : cell.name}>
                    <PackageList packages={cell.packages} />
                </ReviewSection>)}
                {review.libraries.length > 0 && <ReviewSection label="Libraries">
                    <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-[0.75rem] text-ink-2">
                        {review.libraries.map(row => <li key={row.id}><span className="font-mono text-ink">{row.id}</span>: {resolution(row, review.target.channel)}</li>)}
                    </ul>
                </ReviewSection>}
                {review.skipped.length > 0 && <ReviewSection label="Left out">
                    <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-[0.75rem] text-ink-2">
                        {review.skipped.map(skip => <li key={`${skip.cell_id}:${skip.path}`}><span className="font-mono">{skip.path}</span> in {skip.cell}: {skip.reason}</li>)}
                    </ul>
                </ReviewSection>}
                <button type="button" className={`${btnPrimary} self-start`} disabled={busy} onClick={() => void copy()}>
                    <Copy className="h-3.5 w-3.5" aria-hidden />Copy to ‘{review.target.channel}’
                </button>
            </>}
        </div>
    </SubView>;
}
