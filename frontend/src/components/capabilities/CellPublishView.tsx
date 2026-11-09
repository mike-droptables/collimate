import { useEffect, useState } from 'react';
import { BookUp } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { publishToCatalog, reviewPublish, type PublishLibrary, type PublishReview } from '@/lib/api/userCatalog';
import { btnPrimary } from '@/components/ui/formStyles';
import { PackageList, ReviewSection } from './PackageReview';
import { SubView } from './SubView';

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** Where a library the published cell names resolves for a channel that adds it. */
function resolution(row: PublishLibrary): string {
    switch (row.resolves) {
        case 'self': return 'in this cell';
        case 'app': return 'the app’s';
        case 'catalog': return `in the catalog, in ‘${row.entry}’`;
        default: return row.cell
            ? `not in the catalog: publish ‘${row.cell}’ too, or a channel without it cannot add this`
            : 'not in the catalog: a channel without it cannot add this';
    }
}

/**
 * Publish to catalog, in place of the list: what the catalog keeps of this
 * cell (its packages with Add to's summary; its runners stay here), the
 * libraries its cell.yaml names and where each resolves for a channel that
 * adds it, and whether this publishes a new entry or republishes one
 * (channels that added it then see an update). Publish writes it.
 */
export function CellPublishView({ cellId, cellName, onBack, onDone }: {
    cellId: string; cellName: string; onBack: () => void; onDone: (notice: string) => void;
}) {
    const channelId = useCollimateStore(s => s.activeChannel?.id ?? '');
    const [review, setReview] = useState<PublishReview | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    useEffect(() => {
        let live = true;
        reviewPublish(channelId, cellId).then(
            next => { if (live) setReview(next); },
            e => { if (live) setError(message(e, 'The publish could not be reviewed.')); },
        );
        return () => { live = false; };
    }, [channelId, cellId]);

    const publish = async () => {
        if (!review) return;
        setBusy(true); setError('');
        try {
            const entry = await publishToCatalog(channelId, cellId, review.token);
            onDone(entry.revision > 1 ? `‘${entry.name}’ is republished to the catalog.` : `‘${entry.name}’ is in the catalog.`);
        } catch (e) { setError(message(e, 'The cell could not be published.')); }
        finally { setBusy(false); }
    };

    return <SubView title={`Publish ‘${cellName}’ to the catalog`} onBack={onBack}>
        <div className="flex flex-col gap-3">
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
            {!review && !error && <p className="text-[0.75rem] text-ink-3">Reviewing…</p>}
            {review && <>
                <p className="text-[0.78125rem] text-ink-2">
                    {review.entry
                        ? `Republishes ‘${review.entry.name}’ (revision ${review.entry.revision}): channels that added it see an update.`
                        : 'A new catalog entry. Any channel can add it from the catalog; its runners stay in this channel.'}
                </p>
                <ReviewSection label="Packages"><PackageList packages={review.packages} /></ReviewSection>
                {review.libraries.length > 0 && <ReviewSection label="Libraries">
                    <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-[0.75rem] text-ink-2">
                        {review.libraries.map(row => <li key={row.id}><span className="font-mono text-ink">{row.id}</span>: {resolution(row)}</li>)}
                    </ul>
                </ReviewSection>}
                {review.unchanged
                    ? <p className="text-[0.78125rem] text-ink-2">The catalog already holds this cell as it is.</p>
                    : <button type="button" className={`${btnPrimary} self-start`} disabled={busy} onClick={() => void publish()}>
                        <BookUp className="h-3.5 w-3.5" aria-hidden />{review.entry ? 'Republish' : 'Publish'}
                    </button>}
            </>}
        </div>
    </SubView>;
}
