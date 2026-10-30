import { useEffect, useState } from 'react';
import { Plus } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useRunnerCatalog } from '@/stores/useRunnerStore';
import type { GeneratorEntry } from '@/lib/api';
import { fetchChannelOutline, type OutlineItem } from '@/lib/api/cells';
import { runnerReadinessLabel, type CapabilityGroup as Group } from '@/lib/capabilityList';
import {
    deleteRunner, duplicateRunner, fetchRunner, saveRunner,
    type RunnerDraft, type RunnerEditorPayload, type RunnerEntry, type RunnerGenerator, type RunnerHome,
} from '@/lib/runnerApi';
import { RunnerEditor } from '@/components/runner/RunnerEditor';
import { btn, btnDanger, btnPrimary, card, chip, confirmStrip, formLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import type { CatalogSlot } from './useCapabilityCatalogs';
import { CapabilityGroup } from './CapabilityGroup';
import { SubView } from './SubView';

const STATUS_TONE = { ready: 'border-green text-green', setup: 'border-brass text-ink-2', broken: 'border-red text-red' } as const;

/**
 * The Runners group: one card per runner (its name, the generator it runs,
 * its description and tags, and whether it is ready), with Edit,
 * Duplicate, Delete (confirmed in a strip) and Open cell. 'Add runner'
 * starts a new runner (`onAdd`); Edit opens the runner editor in place of
 * the list (`onEdit`). With no generator in the channel there is nothing a
 * runner could start: 'Add runner' is off, and the group offers Create…
 * (`onCreate`) instead.
 */
export function RunnersGroup({ group, filtering, slot, generators, cellName, onAdd, onCreate, onEdit, onShowCell }: {
    group: Group<'runner'>;
    filtering: boolean;
    slot: CatalogSlot<RunnerEntry[]>;
    /** The channel's generators, null while they load. */
    generators: readonly GeneratorEntry[] | null;
    cellName: (cellId: string) => string;
    onAdd: () => void;
    onCreate: () => void;
    onEdit: (payload: RunnerEditorPayload) => void;
    onShowCell: (cellId: string) => void;
}) {
    const catalog = useRunnerCatalog();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [confirming, setConfirming] = useState<string | null>(null);
    const work = async (fallback: string, fn: () => Promise<void>) => {
        setBusy(true); setError('');
        try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : fallback); } finally { setBusy(false); }
    };
    const generatorName = (id: string) => generators?.find(generator => generator.name === id)?.display_name ?? id;
    const noGenerators = !!generators && generators.length === 0;
    const empty = noGenerators
        ? <div className="flex flex-col items-start gap-2">
            <p>Runners start a generator. Add a generator first.</p>
            <button type="button" className={btn} onClick={onCreate}>Create…</button>
        </div>
        : 'No runners yet.';
    return <CapabilityGroup kind="runner" title="Runners" subtitle="Start a generator with saved settings."
        shown={group.user.length} total={group.total} filtering={filtering}
        loading={!slot.data && !slot.error} error={slot.error} onRetry={slot.retry} empty={empty}
        action={<button type="button" className={btn} disabled={busy || noGenerators} onClick={onAdd}
            title={noGenerators ? 'Runners start a generator: add a generator first' : undefined}>
            <Plus className="h-3.5 w-3.5" aria-hidden />Add runner
        </button>}>
        {error && <p role="alert" className="px-1 text-[0.75rem] text-red">{error}</p>}
        {group.user.length > 0 && <div data-runner-list="" className="flex flex-col gap-2">
            {group.user.map(({ item: runner }) => {
                const status = runnerReadinessLabel(runner);
                const holder = cellName(runner.cell_id) || 'its cell';
                return <article key={runner.id} data-runner-card={runner.id} aria-label={`${runner.name} runner`} className={cn(card, 'overflow-hidden')}>
                    <div className="flex flex-col gap-1.5 p-3">
                        <div className="flex min-w-0 items-center gap-2">
                            <span className="min-w-0 flex-1 truncate text-[0.8125rem] font-semibold text-ink">{runner.name}</span>
                            <span data-runner-status={status.tone} className={cn(chip, STATUS_TONE[status.tone], 'max-w-[60%] truncate')} title={status.label}>{status.label}</span>
                        </div>
                        <span className="text-[0.75rem] text-ink-3">runs {generatorName(runner.generator)}</span>
                        {runner.description && <span className="text-[0.78125rem] leading-[1.45] text-ink-2">{runner.description}</span>}
                        {runner.tags.length > 0 && <span className="flex flex-wrap gap-1">{runner.tags.map(tag => <span key={tag} className={chip}>{tag}</span>)}</span>}
                        <div className="mt-1 flex flex-wrap gap-1.5">
                            <button type="button" className={btn} disabled={busy}
                                onClick={() => void work('Could not open the runner', async () => onEdit(await fetchRunner(runner.id)))}>Edit</button>
                            <button type="button" className={btn} disabled={busy}
                                onClick={() => void work('Could not duplicate the runner', async () => {
                                    await duplicateRunner(runner.id, (catalog.data ?? []).map(item => item.name));
                                    await catalog.refresh();
                                })}>Duplicate</button>
                            <button type="button" className={btn} disabled={busy} onClick={() => setConfirming(runner.id)}>Delete</button>
                            <button type="button" className={btn} onClick={() => onShowCell(runner.cell_id)}>Open cell</button>
                        </div>
                    </div>
                    {confirming === runner.id && <div className={confirmStrip}>
                        <span className="min-w-0 flex-1">Delete ‘{runner.name}’? Its files leave {holder}.</span>
                        <button type="button" className={btn} onClick={() => setConfirming(null)}>Keep</button>
                        <button type="button" className={btnDanger} disabled={busy}
                            onClick={() => void work('Could not delete the runner', async () => {
                                await deleteRunner(runner.id);
                                await catalog.refresh();
                                setConfirming(null);
                            })}>Delete</button>
                    </div>}
                </article>;
            })}
        </div>}
    </CapabilityGroup>;
}

/** A new runner's draft for `generator`: its default settings, nameless,
 *  carrying `description` when one was given. */
function startDraft(generator: RunnerGenerator, description?: string): RunnerDraft {
    const template = structuredClone(generator.default);
    return {
        runner: { ...template.runner, id: '', generator: generator.id, name: '', description: description || template.runner.description },
        files: template.files,
    };
}

/** The 'Keep it in' choice: its generator's cell (the default), a new
 *  cell on Capabilities, or a Capabilities cell by id. */
type Home = { kind: 'generator' } | { kind: 'new' } | { kind: 'cell'; cellId: string };

/** What saveRunner sends for the choice (nothing: its generator's cell). */
const homeTarget = (home: Home): RunnerHome | undefined =>
    home.kind === 'new' ? { newCell: true } : home.kind === 'cell' ? { cellId: home.cellId } : undefined;

/**
 * Add a runner, in place of the list: first 'Keep it in' (its generator's
 * cell, the default; any other Capabilities cell; or a new cell), then the
 * runner editor, from the generator pick or with `generatorId` preselected.
 */
export function RunnerAddView({ generatorId, description, generators, onBack, onSaved }: {
    generatorId?: string;
    description?: string;
    /** The generators' runner schemas, null while they load. */
    generators: RunnerGenerator[] | null;
    onBack: () => void;
    onSaved: (notice: string) => void;
}) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const catalog = useRunnerCatalog();
    const [cells, setCells] = useState<OutlineItem[] | null>(null);
    const [error, setError] = useState('');
    const [home, setHome] = useState<Home>({ kind: 'generator' });
    const [chosen, setChosen] = useState(false);
    useEffect(() => {
        let live = true;
        fetchChannelOutline(channel)
            .then(outline => { if (live) setCells(outline.capabilities_cells); })
            .catch(e => { if (live) setError(e instanceof Error ? e.message : 'Could not list the Capabilities cells'); });
        return () => { live = false; };
    }, [channel]);
    const generator = generatorId ? generators?.find(item => item.id === generatorId) : undefined;
    const title = generator ? `Add a runner of ‘${generator.name}’` : 'Add a runner';
    const choices: { key: string; home: Home; name: string }[] = [
        { key: 'generator-cell', home: { kind: 'generator' }, name: 'Its generator’s cell' },
        ...(cells ?? []).map(cell => ({ key: cell.id, home: { kind: 'cell', cellId: cell.id } as Home, name: cell.name || 'Untitled cell' })),
        { key: 'new-cell', home: { kind: 'new' }, name: 'New cell' },
    ];
    const same = (a: Home, b: Home) => a.kind === b.kind && (a.kind !== 'cell' || a.cellId === (b as { cellId: string }).cellId);

    if (!chosen) return <SubView title={title} onBack={onBack}>
        <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
            <legend className={cn(formLabel, 'mb-1')}>Keep it in</legend>
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
            {choices.map(choice =>
                <label key={choice.key} className="flex items-center gap-2 rounded-md px-1 py-1 text-[0.78125rem] text-ink hover:bg-sheet-2">
                    <input type="radio" name="runner-home" className="accent-teal" checked={same(home, choice.home)} onChange={() => setHome(choice.home)} />
                    <span className="min-w-0 flex-1 truncate">{choice.name}</span>
                </label>)}
            {!cells && !error && <p className="text-[0.75rem] text-ink-3">Loading the Capabilities cells…</p>}
            {home.kind === 'new' && <p className="px-1 text-[0.75rem] text-ink-3">A new cell at the end of Capabilities, named after the runner.</p>}
        </fieldset>
        <div className="mt-3 flex justify-end gap-2 border-t border-line pt-3">
            <button type="button" className={btn} onClick={onBack}>Cancel</button>
            <button type="button" className={btnPrimary} disabled={!!generatorId && !generator} onClick={() => setChosen(true)}>Next</button>
        </div>
    </SubView>;

    return <SubView title={title} onBack={onBack} scroll={false}>
        <RunnerEditor key={generator?.id ?? 'pick'} initial={generator ? startDraft(generator, description) : undefined} generators={generators ?? undefined}
            onCancel={onBack}
            onSave={async draft => {
                await saveRunner(draft, false, homeTarget(home));
                await catalog.refresh();
                onSaved(`Runner ‘${draft.runner.name}’ added.`);
            }} />
    </SubView>;
}

/** Edit a runner, in place of the list; Save writes it where it lives. */
export function RunnerEditView({ payload, onBack, onSaved }: {
    payload: RunnerEditorPayload;
    onBack: () => void;
    onSaved: (notice: string) => void;
}) {
    const catalog = useRunnerCatalog();
    return <SubView title={`Edit ‘${payload.runner.name}’`} onBack={onBack} scroll={false}>
        <RunnerEditor key={payload.runner.id} initial={payload} onCancel={onBack}
            onSave={async draft => {
                await saveRunner(draft, true);
                await catalog.refresh();
                onSaved(`Runner ‘${draft.runner.name}’ saved.`);
            }} />
    </SubView>;
}
