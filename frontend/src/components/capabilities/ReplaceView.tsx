import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { replaceFromUpstream, reviewReplace, type ReplaceReview } from '@/lib/api/capabilityCells';
import { fetchState } from '@/lib/api';
import { btnPrimary, chip } from '@/components/ui/formStyles';
import { PackageList, ReviewSection } from './PackageReview';
import { SubView } from './SubView';

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

const CHANGE: Record<ReplaceReview['packages'][number]['change'], string> = {
    added: 'new', changed: 'changed', same: 'unchanged', removed: 'goes',
};

const RESOLVES = { cell: 'in this cell', capabilities: 'on Capabilities', app: 'the app’s' } as const;

/**
 * Review and replace, in place of the list: the upstream's current copy of
 * the cell, admitted in Replace mode. It lists each package and how it
 * changes (with Add to's summary for the new copy), the runners kept, the
 * libraries the new cell.yaml names and where each resolves, for a library
 * the cell holds the cells that name it (they run the new copy from their
 * next run), and says when edits made here are discarded. Replace makes the
 * new version of the same cell.
 */
export function ReplaceView({ cellId, cellName, onBack, onDone }: {
    cellId: string; cellName: string; onBack: () => void; onDone: (notice: string) => void;
}) {
    const channelId = useCollimateStore(s => s.activeChannel?.id ?? '');
    const [review, setReview] = useState<ReplaceReview | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    useEffect(() => {
        let live = true;
        reviewReplace(channelId, cellId).then(
            next => { if (live) setReview(next); },
            e => { if (live) setError(message(e, 'The replace could not be reviewed.')); },
        );
        return () => { live = false; };
    }, [channelId, cellId]);

    const replace = async () => {
        if (!review) return;
        setBusy(true); setError('');
        try {
            await replaceFromUpstream(channelId, cellId, review.token);
            useCollimateStore.getState().applyServerState(await fetchState());
            onDone(`‘${cellName}’ holds the current copy from ${review.upstream.label ?? 'its upstream'}.`);
        } catch (e) { setError(message(e, 'The cell could not be replaced.')); }
        finally { setBusy(false); }
    };

    const named = review?.named_by.filter(row => row.cells.length) ?? [];
    return <SubView title={`Review and replace ‘${cellName}’`} onBack={onBack}>
        <div className="flex flex-col gap-3">
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
            {!review && !error && <p className="text-[0.75rem] text-ink-3">Reviewing…</p>}
            {review && <>
                <p className="text-[0.78125rem] text-ink-2">The current copy from {review.upstream.label ?? 'its upstream'} replaces this cell’s definitions as a new version of the same cell.</p>
                {review.edited && <p role="note" className="rounded-md border border-brass px-2.5 py-1.5 text-[0.75rem] text-ink">
                    This cell was edited since it was added: replacing discards those edits (its history keeps them).
                </p>}
                <ReviewSection label="Packages">
                    <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[0.75rem] text-ink-2">
                        {review.packages.map(row => <li key={`${row.kind}:${row.id}`} data-change={row.change}>
                            <span className={chip}>{CHANGE[row.change]}</span> {row.kind} <span className="font-mono text-ink">{row.id}</span>
                        </li>)}
                    </ul>
                    <PackageList packages={review.packages.filter(row => row.change !== 'removed')} />
                </ReviewSection>
                <ReviewSection label="Runners kept">
                    <p className="text-[0.75rem] text-ink-2">{review.runners.length ? review.runners.join(', ') : 'This cell keeps no runner.'}</p>
                </ReviewSection>
                {review.libraries.length > 0 && <ReviewSection label="Libraries">
                    <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-[0.75rem] text-ink-2">
                        {review.libraries.map(row => <li key={row.id}><span className="font-mono text-ink">{row.id}</span>: {row.resolves ? RESOLVES[row.resolves] : 'resolves nowhere'}</li>)}
                    </ul>
                </ReviewSection>}
                {named.length > 0 && <ReviewSection label="Named by">
                    <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-[0.75rem] text-ink-2">
                        {named.map(row => <li key={row.id}><span className="font-mono text-ink">{row.id}</span> is named by {row.cells.map(name => `‘${name}’`).join(', ')}: they run the new copy from their next run.</li>)}
                    </ul>
                </ReviewSection>}
                <button type="button" className={`${btnPrimary} self-start`} disabled={busy} onClick={() => void replace()}>
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden />Replace
                </button>
            </>}
        </div>
    </SubView>;
}
