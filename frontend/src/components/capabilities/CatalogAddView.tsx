import { useEffect, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { addFromCatalog, fetchCapabilityCatalog, type CapabilityCatalogCell, type CapabilityCatalogSuggestion } from '@/lib/api/capabilitiesStack';
import { btn, chip } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { SubView } from './SubView';

/** One cell of the catalog, as this step lists it. */
interface CatalogRow extends CapabilityCatalogCell {
    suggestion: Pick<CapabilityCatalogSuggestion, 'id' | 'name'>;
}

const SECTION_LABEL = { generator: 'generators', renderer: 'renderers and handlers' } as const;

/** The add route's 409 detail for a cell the channel already holds. */
const ALREADY_HERE = 'Already in this channel';

/**
 * Add from catalog, in place of the list: every cell of the suggested
 * catalog (both sections), each with what it holds and, among several
 * suggestions, which one it comes from. A cell the channel already holds
 * (the server's `held`: its packages are already on Capabilities) reads
 * 'Already here'.
 * Add puts the cell on Capabilities and brings it to the front
 * (`onAdded`); a refused add shows the server's reason.
 */
export function CatalogAddView({ onBack, onAdded }: { onBack: () => void; onAdded: (cellId: string, name: string) => void }) {
    const channelId = useCollimateStore(s => s.activeChannel?.id);
    const [rows, setRows] = useState<CatalogRow[] | null>(null);
    const [several, setSeveral] = useState(false);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState<string | null>(null);
    useEffect(() => {
        if (!channelId) return;
        let live = true;
        fetchCapabilityCatalog(channelId)
            .then(suggestions => {
                if (!live) return;
                setRows(suggestions.flatMap(suggestion => suggestion.cells.map(cell => ({ ...cell, suggestion }))));
                setSeveral(suggestions.length > 1);
            })
            .catch(e => { if (live) setError(e instanceof Error ? e.message : 'Could not read the catalog'); });
        return () => { live = false; };
    }, [channelId]);

    const add = async (row: CatalogRow) => {
        if (!channelId) return;
        setBusy(row.folder); setError('');
        try {
            const result = await addFromCatalog(channelId, { suggestion_id: row.suggestion.id, folder: row.folder });
            useCollimateStore.getState().applyOptimisticLayout(result);
            onAdded(result.cell_id, row.name);
        } catch (e) {
            const message = e instanceof Error ? e.message : 'Could not add the cell';
            setError(message === ALREADY_HERE ? `‘${row.name}’ is already in this channel.` : message);
        } finally { setBusy(null); }
    };

    return <SubView title="Add from catalog" onBack={onBack}>
        <p className="mb-2.5 text-[0.78125rem] leading-[1.45] text-ink-2">Cells from the suggested catalog. Adding one puts it on Capabilities, ready to use.</p>
        {error && <p role="alert" className="mb-2 text-[0.75rem] text-red">{error}</p>}
        {!rows && !error && <p className="text-[0.75rem] text-ink-3">Loading the catalog…</p>}
        {rows?.length === 0 && !error && <p className="text-[0.75rem] text-ink-3">The catalog has no cells.</p>}
        {rows && rows.length > 0 && <ul aria-label="Catalog cells" className="m-0 flex list-none flex-col p-0">
            {rows.map(row => (
                <li key={`${row.suggestion.id}:${row.section}:${row.folder}`} data-catalog-cell={row.folder}
                    className="flex items-center gap-2 border-t border-line px-1 py-2.5 last:border-b">
                    <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate font-semibold text-ink">{row.name}</span>
                        <span className="flex flex-wrap items-center gap-1.5 text-[0.71875rem] text-ink-3">
                            <span className={chip}>{SECTION_LABEL[row.section]}</span>
                            {several && <span>from {row.suggestion.name}</span>}
                        </span>
                    </span>
                    {row.held
                        ? <button type="button" className={cn(btn, 'opacity-60')} disabled>Already here</button>
                        : <button type="button" className={btn} disabled={busy !== null} onClick={() => void add(row)}>Add</button>}
                </li>
            ))}
        </ul>}
    </SubView>;
}
