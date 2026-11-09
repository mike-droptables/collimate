import { useCallback, useEffect, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { addFromCatalog, fetchCapabilityCatalog, type CapabilityCatalogCell, type CapabilityCatalogSuggestion } from '@/lib/api/capabilitiesStack';
import {
    addCatalogEntry, deleteSuggestion, fetchCatalogEntries, fetchSavedSuggestions, removeCatalogEntry,
    type ChannelCatalogEntry, type SavedSuggestion,
} from '@/lib/api/userCatalog';
import { reportPackageSkips } from '@/lib/packageReports';
import { btn, btnDanger, chip, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { SubView } from './SubView';
import { SuggestionEditor, type EditorCell } from './SuggestionEditor';

/** One cell of the catalog, as this step lists it. */
interface CatalogRow extends CapabilityCatalogCell {
    suggestion: Pick<CapabilityCatalogSuggestion, 'id' | 'name'>;
}

const SECTION_LABEL = { generator: 'generators', renderer: 'renderers and handlers', library: 'library' } as const;

/** The add route's 409 detail for a cell the channel already holds. */
const ALREADY_HERE = 'Already in this channel';

const ROW = 'flex items-center gap-2 border-t border-line px-1 py-2.5 last:border-b';
const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/**
 * Add from catalog, in place of the list. Your catalog first: the
 * definitions published from any channel (Publish to catalog… on a
 * Capabilities cell's Manage tab), each added with the catalog library
 * entries it needs and removable from the catalog. Then every cell of the
 * app's suggested catalog (generators, renderers, libraries), each with
 * what it holds and, among several suggestions, which one it comes from. A
 * cell the channel already holds (the server's `held`) reads 'Already
 * here'. Add puts the cell on Capabilities and brings it to the front
 * (`onAdded`); a refused add shows the server's reason. Last the saved
 * suggestions, which the New channel page offers beside the app's: Save a
 * suggestion… lists catalog cells and this channel's runners.
 */
export function CatalogAddView({ onBack, onAdded }: { onBack: () => void; onAdded: (cellId: string, name: string) => void }) {
    const channelId = useCollimateStore(s => s.activeChannel?.id);
    const [rows, setRows] = useState<CatalogRow[] | null>(null);
    const [entries, setEntries] = useState<ChannelCatalogEntry[] | null>(null);
    const [saved, setSaved] = useState<SavedSuggestion[] | null>(null);
    const [several, setSeveral] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [busy, setBusy] = useState<string | null>(null);
    const [editing, setEditing] = useState<SavedSuggestion | 'new' | null>(null);
    useEffect(() => {
        if (!channelId) return;
        let live = true;
        fetchCapabilityCatalog(channelId)
            .then(suggestions => {
                if (!live) return;
                setRows(suggestions.flatMap(suggestion => suggestion.cells.map(cell => ({ ...cell, suggestion }))));
                setSeveral(suggestions.length > 1);
            })
            .catch(e => { if (live) setError(message(e, 'Could not read the catalog')); });
        return () => { live = false; };
    }, [channelId]);
    const loadOwn = useCallback(() => {
        if (!channelId) return;
        fetchCatalogEntries(channelId).then(setEntries, e => setError(message(e, 'Could not read your catalog')));
        fetchSavedSuggestions().then(setSaved, e => setError(message(e, 'Could not read the saved suggestions')));
    }, [channelId]);
    useEffect(loadOwn, [loadOwn]);

    const add = async (row: CatalogRow) => {
        if (!channelId) return;
        setBusy(row.folder); setError('');
        try {
            const result = await addFromCatalog(channelId, { suggestion_id: row.suggestion.id, folder: row.folder });
            useCollimateStore.getState().applyOptimisticLayout(result);
            onAdded(result.cell_id, row.name);
        } catch (e) {
            const text = message(e, 'Could not add the cell');
            setError(text === ALREADY_HERE ? `‘${row.name}’ is already in this channel.` : text);
        } finally { setBusy(null); }
    };
    const addEntry = async (entry: ChannelCatalogEntry) => {
        if (!channelId) return;
        setBusy(entry.id); setError('');
        try {
            const result = await addCatalogEntry(channelId, entry.id);
            await reportPackageSkips(result.package_skips);
            useCollimateStore.getState().applyOptimisticLayout(result);
            onAdded(result.cell_id, entry.name);
        } catch (e) {
            const text = message(e, 'Could not add the entry');
            setError(text === ALREADY_HERE ? `‘${entry.name}’ is already in this channel.` : text);
        } finally { setBusy(null); }
    };
    const removeEntry = async (entry: ChannelCatalogEntry) => {
        setBusy(entry.id); setError('');
        try { await removeCatalogEntry(entry.id); setNotice(`‘${entry.name}’ is out of the catalog.`); loadOwn(); }
        catch (e) { setError(message(e, 'Could not remove the entry')); }
        finally { setBusy(null); }
    };
    const removeSuggestion = async (suggestion: SavedSuggestion) => {
        setBusy(suggestion.id); setError('');
        try { await deleteSuggestion(suggestion.id); setNotice(`‘${suggestion.name}’ is deleted.`); loadOwn(); }
        catch (e) { setError(message(e, 'Could not delete the suggestion')); }
        finally { setBusy(null); }
    };

    // The cells a saved suggestion can list: your catalog's, then the app's (each once).
    const editorCells: EditorCell[] = [
        ...(entries ?? []).map(entry => ({ key: `catalog:${entry.id}`, label: entry.name, detail: `your catalog · ${SECTION_LABEL[entry.section]}`,
            item: { source: 'catalog' as const, id: entry.id } })),
        ...[...new Map((rows ?? []).map(row => [`app:${row.section}:${row.folder}`, row])).entries()].map(([key, row]) => ({
            key, label: row.name, detail: `the app · ${SECTION_LABEL[row.section]}`,
            item: { source: 'app' as const, section: row.section, folder: row.folder, name: row.name } })),
    ];

    if (editing && channelId) {
        return <SubView title={editing === 'new' ? 'Save a suggestion' : `Edit ‘${editing.name}’`} onBack={() => setEditing(null)}>
            <SuggestionEditor channelId={channelId} cells={editorCells} editing={editing === 'new' ? null : editing}
                onCancel={() => setEditing(null)} onSaved={text => { setEditing(null); setNotice(text); loadOwn(); }} />
        </SubView>;
    }

    return <SubView title="Add from catalog" onBack={onBack}>
        <p className="mb-2.5 text-[0.78125rem] leading-[1.45] text-ink-2">Cells from your catalog and the app’s suggested catalog. Adding one puts it on Capabilities, ready to use.</p>
        {error && <p role="alert" className="mb-2 text-[0.75rem] text-red">{error}</p>}
        {notice && <p role="status" className="mb-2 text-[0.75rem] text-ink-2">{notice}</p>}
        {entries && entries.length > 0 && <>
            <h4 className={cn(sectionLabel, 'mb-1')}>Your catalog</h4>
            <ul aria-label="Your catalog" className="m-0 mb-3 flex list-none flex-col p-0">
                {entries.map(entry => <li key={entry.id} data-catalog-entry={entry.id} className={ROW}>
                    <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate font-semibold text-ink">{entry.name}</span>
                        <span className="flex flex-wrap items-center gap-1.5 text-[0.71875rem] text-ink-3">
                            <span className={chip}>{SECTION_LABEL[entry.section]}</span>
                            <span>revision {entry.revision}</span>
                            {entry.libraries.length > 0 && <span>names {entry.libraries.join(', ')}</span>}
                        </span>
                    </span>
                    <button type="button" className={btnDanger} disabled={busy !== null} onClick={() => void removeEntry(entry)}
                        aria-label={`Remove ‘${entry.name}’ from the catalog`}>Remove</button>
                    {entry.held
                        ? <button type="button" className={cn(btn, 'opacity-60')} disabled>Already here</button>
                        : <button type="button" className={btn} disabled={busy !== null} onClick={() => void addEntry(entry)}
                            aria-label={`Add ‘${entry.name}’`}>Add</button>}
                </li>)}
            </ul>
            <h4 className={cn(sectionLabel, 'mb-1')}>From the app</h4>
        </>}
        {!rows && !error && <p className="text-[0.75rem] text-ink-3">Loading the catalog…</p>}
        {rows?.length === 0 && !error && <p className="text-[0.75rem] text-ink-3">The catalog has no cells.</p>}
        {rows && rows.length > 0 && <ul aria-label="Catalog cells" className="m-0 flex list-none flex-col p-0">
            {rows.map(row => (
                <li key={`${row.suggestion.id}:${row.section}:${row.folder}`} data-catalog-cell={row.folder} className={ROW}>
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
        <h4 className={cn(sectionLabel, 'mb-1 mt-4')}>Saved suggestions</h4>
        <p className="mb-1.5 text-[0.75rem] text-ink-2">A saved suggestion starts a new channel with the catalog cells and runners it lists, beside the app’s suggestions.</p>
        {saved && saved.length > 0 && <ul aria-label="Saved suggestions" className="m-0 mb-2 flex list-none flex-col p-0">
            {saved.map(suggestion => <li key={suggestion.id} data-saved-suggestion={suggestion.id} className={ROW}>
                <span className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate font-semibold text-ink">{suggestion.name}</span>
                    <span className="text-[0.71875rem] text-ink-3">{suggestion.problem ?? `${suggestion.items.length} cells · ${suggestion.runners.length} runners`}</span>
                </span>
                <button type="button" className={btn} disabled={busy !== null} onClick={() => setEditing(suggestion)}>Edit</button>
                <button type="button" className={btnDanger} disabled={busy !== null} onClick={() => void removeSuggestion(suggestion)}
                    aria-label={`Delete ‘${suggestion.name}’`}>Delete</button>
            </li>)}
        </ul>}
        <button type="button" className={btn} disabled={!rows && !entries} onClick={() => setEditing('new')}>Save a suggestion…</button>
    </SubView>;
}
