import { useState } from 'react';
import { useRunnerCatalog } from '@/stores/useRunnerStore';
import { saveSuggestion, type SavedSuggestion, type SuggestionItem } from '@/lib/api/userCatalog';
import { btn, btnPrimary, formInput, formLabel, formTextarea, sectionLabel } from '@/components/ui/formStyles';

/** A cell the editor can list: an app catalog cell or a catalog entry. */
export interface EditorCell { key: string; label: string; detail: string; item: SuggestionItem }

const itemKey = (item: SuggestionItem) => (item.source === 'app' ? `app:${item.section}:${item.folder}` : `catalog:${item.id}`);

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/**
 * The saved-suggestion editor: a name and a description, the catalog cells
 * it lists (the app's and the user catalog's), and the runners of this
 * channel it offers as drafts. Saving lists any catalog library its
 * entries need too (the server says which); a runner whose generator no
 * listed cell holds is refused with the reason. The suggestion then shows
 * on the New channel page beside the app's.
 */
export function SuggestionEditor({ channelId, cells, editing, onCancel, onSaved }: {
    channelId: string;
    cells: EditorCell[];
    /** The saved suggestion being edited, or none for a new one. */
    editing: SavedSuggestion | null;
    onCancel: () => void;
    onSaved: (notice: string) => void;
}) {
    const runners = useRunnerCatalog().data ?? [];
    const [name, setName] = useState(editing?.name ?? '');
    const [description, setDescription] = useState(editing?.description ?? '');
    const [picked, setPicked] = useState<Set<string>>(() => new Set(editing?.items.map(itemKey) ?? []));
    const [runnerIds, setRunnerIds] = useState<Set<string>>(() => new Set(editing?.runners.map(r => r.draft.runner.id) ?? []));
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const toggle = (set: Set<string>, key: string) => { const next = new Set(set); if (!next.delete(key)) next.add(key); return next; };

    const save = async () => {
        setBusy(true); setError('');
        try {
            const result = await saveSuggestion({
                name: name.trim(), description: description.trim(),
                items: cells.filter(cell => picked.has(cell.key)).map(cell => cell.item),
                channel_id: channelId, runner_ids: runners.filter(r => runnerIds.has(r.id)).map(r => r.id),
            }, editing?.id);
            const added = result.added.length ? ` It lists ${result.added.map(n => `‘${n}’`).join(', ')} too, which its entries need.` : '';
            onSaved(`‘${name.trim()}’ is saved: the New channel page offers it.${added}`);
        } catch (e) { setError(message(e, 'The suggestion could not be saved.')); }
        finally { setBusy(false); }
    };

    return <form aria-label="Saved suggestion" className="flex flex-col gap-3" onSubmit={e => { e.preventDefault(); void save(); }}>
        <label className="flex flex-col gap-1"><span className={formLabel}>Name</span>
            <input className={formInput} value={name} disabled={busy} onChange={e => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1"><span className={formLabel}>Description</span>
            <textarea className={formTextarea} rows={2} value={description} disabled={busy} onChange={e => setDescription(e.target.value)} />
        </label>
        <fieldset className="flex flex-col gap-1"><legend className={sectionLabel}>Cells</legend>
            {cells.map(cell => <label key={cell.key} className="flex items-center gap-2 text-[0.78125rem] text-ink-2">
                <input type="checkbox" className="accent-teal" checked={picked.has(cell.key)} disabled={busy}
                    onChange={() => setPicked(set => toggle(set, cell.key))} />
                <span className="text-ink">{cell.label}</span><span className="text-ink-3">{cell.detail}</span>
            </label>)}
        </fieldset>
        <fieldset className="flex flex-col gap-1"><legend className={sectionLabel}>Runners from this channel</legend>
            {runners.length === 0 && <p className="text-[0.75rem] text-ink-3">This channel has no runners.</p>}
            {runners.map(runner => <label key={runner.id} className="flex items-center gap-2 text-[0.78125rem] text-ink-2">
                <input type="checkbox" className="accent-teal" checked={runnerIds.has(runner.id)} disabled={busy}
                    onChange={() => setRunnerIds(set => toggle(set, runner.id))} />
                <span className="text-ink">{runner.name}</span><span className="font-mono text-[0.71875rem] text-ink-3">{runner.generator}</span>
            </label>)}
        </fieldset>
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        <div className="flex gap-2">
            <button type="button" className={btn} disabled={busy} onClick={onCancel}>Cancel</button>
            <button type="submit" className={btnPrimary} disabled={busy || !name.trim() || picked.size === 0}>Save suggestion</button>
        </div>
    </form>;
}
