import { ArrowUpCircle } from 'lucide-react';
import type { CellUpstream } from '@/lib/api/capabilityCells';
import { btn } from '@/components/ui/formStyles';

/** What a cell's Manage tab says about where its packages came from. */
export function UpstreamLine({ follow, onReview }: { follow: CellUpstream | undefined; onReview: () => void }) {
    if (!follow) return <p className="text-[0.75rem] text-ink-3">Follows no upstream: it changes only where you edit it.</p>;
    const label = follow.upstream.label ?? 'its upstream';
    return <div data-cell-upstream={follow.status} className="flex flex-col gap-1 text-[0.75rem] text-ink-2">
        <span>Follows {label}.</span>
        {follow.status === 'update' && <span className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 font-medium text-teal"><ArrowUpCircle className="size-3.5" aria-hidden />Update available</span>
            <button type="button" className={btn} onClick={onReview}>Review and replace…</button>
        </span>}
        {follow.status === 'missing' && <span>Its upstream no longer holds it: nothing to update from.</span>}
        {follow.status === 'mixed' && <span>Its packages come from more than one upstream: update them through Add to.</span>}
        {follow.edited && follow.status !== 'mixed' && <span className="text-ink-3">Edited here since it was added.</span>}
    </div>;
}

/** The master face's line of cells whose upstream changed, each with
 *  Review and replace…; nothing shows while there are none. */
export function UpdatesStrip({ updates, cellName, onReview }: {
    updates: readonly CellUpstream[];
    cellName: (cellId: string) => string;
    onReview: (cellId: string) => void;
}) {
    const waiting = updates.filter(row => row.status === 'update');
    if (!waiting.length) return null;
    return <section aria-label="Updates available" className="flex flex-col gap-1.5 rounded-md border border-teal px-3 py-2">
        <span className="flex items-center gap-1.5 text-[0.78125rem] text-ink">
            <ArrowUpCircle className="size-3.5 text-teal" aria-hidden />
            {waiting.length === 1 ? 'An update is available' : `${waiting.length} updates are available`}
        </span>
        <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {waiting.map(row => <li key={row.cell_id} data-update={row.cell_id} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 text-[0.75rem] text-ink-2"><span className="text-ink">{cellName(row.cell_id) || 'A cell'}</span> from {row.upstream.label ?? 'its upstream'}</span>
                <button type="button" className={btn} onClick={() => onReview(row.cell_id)}>Review and replace…</button>
            </li>)}
        </ul>
    </section>;
}
