import { useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { saveRendererPreferences, setRendererAvailability, type RendererEntry } from '@/lib/api';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import type { CapabilityGroup as Group } from '@/lib/capabilityList';
import { definitionText, parseDefinition, replaceFiles, textFile } from '@/lib/definitionFiles';
import { readCellText } from '@/lib/fileCommands';
import { RendererCard } from '@/components/cellEditor/RendererCard';
import type { CatalogSlot } from './useCapabilityCatalogs';
import { BuiltinDisclosure, CapabilityGroup } from './CapabilityGroup';
import { announceRendererChange } from './manageActions';

/** Rewrite a user renderer's `.renderer` name or summary in the cell that
 *  holds it, only if the definition is still the one read. */
async function writeRendererDisplay(channel: string, renderer: RendererEntry, patch: { name?: string; summary?: string }): Promise<void> {
    const cellId = renderer.cell_id;
    if (!cellId) throw new Error('The renderer’s cell is unavailable.');
    await flushCellSaves(cellId);
    const store = useCollimateStore.getState();
    let selected: { name: string; revision: unknown; hash: unknown; manifest: Record<string, unknown> } | null = null;
    for (const file of store.locateCell(cellId)?.cell.files.filter(item => item.name.endsWith('.renderer')) ?? []) {
        const manifest = parseDefinition(await readCellText(channel, cellId, file.name));
        if (manifest.id === renderer.id) { selected = { name: file.name, revision: file.file_revision, hash: file.body_hash, manifest }; break; }
    }
    if (!selected) throw new Error('The renderer definition is unavailable.');
    const latest = useCollimateStore.getState().locateCell(cellId)?.cell.files ?? [];
    const now = latest.find(item => item.name === selected.name);
    if (!now || now.file_revision !== selected.revision || now.body_hash !== selected.hash) throw new Error('The renderer definition changed. Retry.');
    const next = replaceFiles(latest, [textFile(selected.name, definitionText({ ...selected.manifest, ...patch }))]);
    if (!await useCollimateStore.getState().applyCellFiles(cellId, next)) throw new Error('Could not save the renderer.');
}

/**
 * The Renderers group: one RendererCard per renderer, the user's first and
 * the app's in a 'Built-in (n)' disclosure. The availability switch and the
 * default chips write the channel's renderer settings; a name or summary
 * edit rewrites the renderer's `.renderer` in the cell that holds it.
 */
export function RendererGroup({ group, filtering, slot, onShowCell }: {
    group: Group<'renderer'>;
    filtering: boolean;
    slot: CatalogSlot<{ renderers: RendererEntry[]; prefs: Record<string, string> }>;
    onShowCell: (cellId: string) => void;
}) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const [builtinOpen, setBuiltinOpen] = useState(false);
    const [error, setError] = useState('');
    // The preferences as last written here, until the reload brings them.
    const loaded = slot.data?.prefs;
    const [written, setWritten] = useState<{ over: Record<string, string> | undefined; prefs: Record<string, string> } | null>(null);
    const prefs = written && written.over === loaded ? written.prefs : loaded ?? {};

    // Surface a failure inline, and always announce: the reload re-syncs
    // from the server, undoing a failed optimistic write.
    const mutate = async (fn: () => Promise<void>) => {
        setError('');
        try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : 'Renderer update failed'); } finally { announceRendererChange(); }
    };
    const writePrefs = (next: Record<string, string>) => mutate(async () => {
        setWritten({ over: loaded, prefs: next });
        await saveRendererPreferences(next);
    });
    const toggleAvailable = (renderer: RendererEntry, enabled: boolean) => mutate(async () => {
        if (!enabled) {
            // A disabled renderer can't be a default: drop each preference
            // naming it before turning it off.
            const cleaned = Object.fromEntries(Object.entries(prefs).filter(([, id]) => id !== renderer.id));
            if (Object.keys(cleaned).length !== Object.keys(prefs).length) {
                setWritten({ over: loaded, prefs: cleaned });
                await saveRendererPreferences(cleaned);
            }
        }
        await setRendererAvailability(renderer.id, enabled);
    });

    const row = (renderer: RendererEntry) => <RendererCard key={renderer.id} renderer={renderer} prefs={prefs}
        onToggleAvailable={enabled => { void toggleAvailable(renderer, enabled); }}
        onSetDefault={ext => { void writePrefs({ ...prefs, [ext]: renderer.id }); }}
        onEditDisplay={patch => { void mutate(() => writeRendererDisplay(channel, renderer, patch)); }}
        onOpenCell={renderer.cell_id ? () => onShowCell(renderer.cell_id!) : undefined} />;

    return <CapabilityGroup kind="renderer" title="Renderers" subtitle="Show files."
        shown={group.user.length + group.builtin.length} total={group.total} filtering={filtering}
        loading={!slot.data && !slot.error} error={slot.error} onRetry={slot.retry}
        empty="No renderers yet. Create one, or add a .renderer from any cell’s Files panel.">
        {error && <p role="alert" className="px-1 text-[0.75rem] text-red">{error}</p>}
        {group.user.length > 0 && <div data-renderer-list="" className="flex flex-col">{group.user.map(entry => row(entry.item))}</div>}
        <BuiltinDisclosure count={group.builtin.length} open={builtinOpen || filtering} onToggle={() => setBuiltinOpen(value => !value)}>
            <div data-renderer-list="builtin" className="flex flex-col">{group.builtin.map(entry => row(entry.item))}</div>
        </BuiltinDisclosure>
    </CapabilityGroup>;
}
