import { lazy, Suspense, useEffect, useRef, useState, type RefObject } from 'react';
import { ArrowUp, Settings } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CapabilitiesManageRequest } from '@/stores/collimate/selectionSlice';
import { capabilityEntries, groupCapabilities, type AnyCapabilityGroup } from '@/lib/capabilityList';
import { fitCatalogs } from '@/lib/capabilityFit';
import type { HandlerInfo } from '@/lib/definitionApi';
import { BARE_KEYS_OWNER_PROPS } from '@/lib/keyboardOwner';
import { fetchRunner, type RunnerEditorPayload } from '@/lib/runnerApi';
import { keyboardIsFree } from '@/lib/selectionFocus';
import type { CapabilityKind } from '@/lib/types';
import { isFocusWheel } from '@/lib/wheelReplay';
import { cn } from '@/lib/utils';
import { GeneratorList } from '@/components/cellEditor/GeneratorRows';
import { HandlerEditView } from '@/components/cellEditor/HandlerEditView';
import { HandlerManagement, type HandlerFocus } from '@/components/cellEditor/HandlerManagement';
import { StackWidthControl } from '@/components/StackWidthControl';
import { ScrollArea } from '@/components/ui/scroll-area';
import { btn, iconBtn } from '@/components/ui/formStyles';
import { CapabilitiesAbout } from './CapabilitiesAbout';
import { CapabilitiesToolbar } from './CapabilitiesToolbar';
import { CapabilityGroup } from './CapabilityGroup';
import { CatalogAddView } from './CatalogAddView';
import type { CreateExit } from './CreateQuestionnaire';
import { ProblemsStrip } from './ProblemsStrip';
import { RendererGroup } from './RendererGroup';
import { RunnerAddView, RunnerEditView, RunnersGroup } from './RunnersGroup';
import { SubView } from './SubView';
import { useCapabilityCatalogs } from './useCapabilityCatalogs';

const CreateQuestionnaire = lazy(() => import('./CreateQuestionnaire'));

/** The Capabilities stack's slot 0 (StackGroup renders it in the card
 *  position, under the same motion layers as a cell card). */
export interface CapabilitiesManageCardProps {
    stackId: string;
    /** The card's size (its motion layer sizes it; the card fills it). */
    width: number;
    height: number;
    /** The Capabilities stack is the selected stack. */
    isSelected: boolean;
    /** The manage card is the stack's front face. */
    isFront: boolean;
    /** A press on the card selects the stack. */
    onSelect(): void;
    /** Bring this Capabilities cell to the front of the stack. */
    onShowCell(cellId: string): void;
    /** Hide Capabilities to its top-bar pill. */
    onHide(): void;
}

/** What replaces the list (component state: it resets when the stack is
 *  hidden or the channel switches). */
type View =
    | { kind: 'list' }
    | { kind: 'runner-add'; generatorId?: string; description?: string }
    | { kind: 'runner-edit'; payload: RunnerEditorPayload }
    | { kind: 'handler-edit'; handler: HandlerInfo; saved: (notice: string) => void }
    | { kind: 'create'; description?: string }
    | { kind: 'catalog' };

const LIST: View = { kind: 'list' };

/** Frames the card waits for the keyboard to come free when fronted (the
 *  selection hand-off releases another cell's keys one frame on). */
const FOCUS_RETRY_FRAMES = 2;

const GROUP_SUBTITLES: Record<CapabilityKind, string> = {
    generator: 'Make files.',
    runner: 'Start a generator with saved settings.',
    renderer: 'Show files.',
    handler: 'Check files and teach agents.',
};

/**
 * The manage view: slot 0 of the Capabilities stack, before the built-in
 * cards and the user's cells. It lists every generator, runner, renderer
 * and handler of the channel and hosts their editors; nothing about it is
 * sent to the server, and it is never dragged.
 *
 * Its root owns its bare keys (BARE_KEYS_OWNER_PROPS): '/' typed in its
 * search never opens Commands, while the scene's chords (Alt+↑/↓ paging,
 * Ctrl+←/→, Mod+K, Alt+Shift+Backspace hiding the stack) still work from
 * it. Fronted on the selected stack, it focuses its search once nothing
 * holds the keyboard (the scene's hand-off follows the selected cell, and
 * the manage card is none; keys left in another cell are released a frame
 * later, so it looks again for two frames). A vertical wheel over it
 * selects the stack and scrolls the list, never the stack.
 */
export function CapabilitiesManageCard({ stackId, isSelected, isFront, onSelect, onShowCell, onHide }: CapabilitiesManageCardProps) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const rootRef = useRef<HTMLDivElement>(null);
    return <div ref={rootRef} {...BARE_KEYS_OWNER_PROPS} data-capabilities-manage="" data-stack-card="" tabIndex={-1}
        aria-label="Manage Capabilities"
        style={{ width: '100%', height: '100%', pointerEvents: 'auto', backgroundColor: isFront ? 'var(--sheet)' : 'var(--canvas)' }}
        className={cn('group/card relative flex flex-col overflow-hidden rounded-lg border outline-none',
            isSelected && isFront ? 'border-brass' : 'border-line', isFront && 'shadow-(--shadow)')}
        onPointerDown={e => { e.stopPropagation(); if (e.button !== 2) onSelect(); }}
        onWheelCapture={e => { if (!isSelected && isFocusWheel(e)) onSelect(); }}>
        <div className={cn('relative flex h-9 flex-none items-center gap-[0.625rem] border-b border-line pl-[0.875rem] pr-[0.625rem]', isFront && 'bg-teal-sheet')}>
            <Settings className="size-3.5 flex-none text-teal" strokeWidth={1.6} aria-hidden />
            <span className={cn('min-w-0 flex-1 truncate text-[0.875rem] font-semibold tracking-[-0.005em]', isFront ? 'text-ink' : 'text-ink-2')}>Manage</span>
            {isFront && <div className="flex flex-none items-center gap-1">
                <StackWidthControl stackId={stackId} onDragStart={onSelect} />
                <button type="button" aria-label="Hide Capabilities" title="Hide Capabilities to the top bar" data-testid="manage-hide-stack"
                    className={cn(iconBtn, 'h-6 w-6')} onClick={e => { e.stopPropagation(); onHide(); }}>
                    <ArrowUp className="h-3.5 w-3.5" strokeWidth={1.6} aria-hidden />
                </button>
            </div>}
        </div>
        <ManageBody key={channel} stackId={stackId} active={isSelected && isFront} rootRef={rootRef} onShowCell={onShowCell} />
    </div>;
}

function ManageBody({ stackId, active, rootRef, onShowCell }: {
    stackId: string;
    active: boolean;
    rootRef: RefObject<HTMLDivElement | null>;
    onShowCell: (cellId: string) => void;
}) {
    const stack = useCollimateStore(s => [...(s.activeChannel?.main_stacks ?? []), ...(s.activeChannel?.nav_stacks ?? [])].find(item => item.id === stackId));
    const cellName = (cellId: string | undefined) => stack?.cells.find(cell => cell.id === cellId)?.name ?? '';
    const catalogs = useCapabilityCatalogs();
    const [view, setView] = useState<View>(LIST);
    const [query, setQuery] = useState('');
    const [kinds, setKinds] = useState<CapabilityKind[]>([]);
    const [focus, setFocus] = useState<HandlerFocus | null>(null);
    const [notice, setNotice] = useState('');
    const [editRunner, setEditRunner] = useState<string | null>(null);
    const searchRef = useRef<HTMLInputElement>(null);

    // Each handler focus takes a fresh seq, from a counter that only goes
    // up (a focus is dropped once its card has shown it).
    const [focusSeq, setFocusSeq] = useState(0);
    const back = () => setView(LIST);
    const focusHandler = (handler: { cellId: string; folder: string; step?: HandlerFocus['step'] }) => {
        setView(LIST); setKinds(['handler']); setQuery('');
        setFocusSeq(focusSeq + 1);
        setFocus({ ...handler, seq: focusSeq + 1 });
    };

    // A request from elsewhere (the store's one-shot capabilitiesManageRequest):
    // applied once, then cleared by its seq.
    const request = useCollimateStore(s => s.capabilitiesManageRequest);
    const [applied, setApplied] = useState<number | null>(null);
    if (request && request.seq !== applied) {
        setApplied(request.seq);
        apply(request);
    }
    function apply(next: CapabilitiesManageRequest) {
        setNotice('');
        if (next.kind) setKinds([next.kind]);
        if (next.query !== undefined) setQuery(next.query);
        if (next.handler) focusHandler(next.handler);
        else if (next.runner?.action === 'edit' && next.runner.runnerId) { setView(LIST); setEditRunner(next.runner.runnerId); }
        else if (next.runner) setView({ kind: 'runner-add', generatorId: next.runner.generatorId, description: next.runner.description });
        else if (next.create) setView({ kind: 'create' });
        else if (next.addFromCatalog) setView({ kind: 'catalog' });
        else setView(LIST);
    }
    useEffect(() => {
        if (request) useCollimateStore.getState().clearCapabilitiesManageRequest(request.seq);
    }, [request]);
    useEffect(() => {
        if (!editRunner) return;
        let live = true;
        fetchRunner(editRunner)
            .then(payload => { if (live) setView({ kind: 'runner-edit', payload }); })
            .catch(e => { if (live) setNotice(e instanceof Error ? e.message : 'Could not open the runner'); })
            .finally(() => { if (live) setEditRunner(null); });
        return () => { live = false; };
    }, [editRunner]);

    // Fronted on the selected stack with the keyboard free: the search (or,
    // under an editor, the card) takes it. Keys another cell still holds are
    // released a frame after the selection moved (useSelectionFocus), so
    // the card looks again on the next two frames.
    const listShown = view.kind === 'list';
    useEffect(() => {
        if (!active) return;
        let raf = 0;
        let retries = FOCUS_RETRY_FRAMES;
        const take = () => {
            if (keyboardIsFree(document)) (listShown ? searchRef.current : rootRef.current)?.focus({ preventScroll: true });
            else if (retries-- > 0) raf = requestAnimationFrame(take);
        };
        take();
        return () => cancelAnimationFrame(raf);
        // Only the moment the card comes to the front, not each view change.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [active]);

    const generators = catalogs.generators.data;
    const renderers = catalogs.renderers.data;
    // Adding a runner with no generator in the channel is a dead end: the
    // Runners group shows why, with Create…, instead.
    if (view.kind === 'runner-add' && generators?.generators.length === 0) {
        setView(LIST);
        setKinds(['runner']);
    }
    const entries = capabilityEntries({
        generators: generators?.generators,
        runners: catalogs.runners.data ?? undefined,
        renderers: renderers?.renderers,
        handlers: catalogs.handlers.data ?? undefined,
    }, cellName);
    const grouped = groupCapabilities(entries, { query, kinds });
    const loading = !generators && !catalogs.runners.data && !renderers && !catalogs.handlers.data
        && !catalogs.generators.error && !catalogs.runners.error && !catalogs.renderers.error && !catalogs.handlers.error;
    const schemas = Object.fromEntries((generators?.runnerGenerators ?? []).map(schema => [schema.id, schema]));
    const done = (text: string) => { setView(LIST); setNotice(text); };

    const exitCreate = (exit: CreateExit) => {
        if (exit.kind === 'handler') focusHandler(exit);
        else if (exit.kind === 'runner') setView({ kind: 'runner-add', generatorId: exit.generatorId, description: exit.description });
        else done(exit.text);
    };

    const group = (g: AnyCapabilityGroup) => {
        if (grouped.filtering && !g.user.length && !g.builtin.length) return null;
        switch (g.kind) {
            case 'generator': return <CapabilityGroup key={g.kind} kind={g.kind} title="Generators" subtitle={GROUP_SUBTITLES.generator}
                shown={g.user.length} total={g.total} filtering={grouped.filtering}
                loading={!generators && !catalogs.generators.error} error={catalogs.generators.error} onRetry={catalogs.generators.retry}
                empty="No generators yet. Create one, or add a .generator from any cell’s Files panel.">
                {g.user.length > 0 && <GeneratorList generators={g.user.map(entry => entry.item)} schemas={schemas} onShowCell={onShowCell}
                    onAddRunner={generator => setView({ kind: 'runner-add', generatorId: generator.name })} />}
            </CapabilityGroup>;
            case 'runner': return <RunnersGroup key={g.kind} group={g} filtering={grouped.filtering} onShowCell={onShowCell}
                slot={catalogs.runners} generators={generators?.generators ?? null} cellName={cellName}
                onAdd={() => setView({ kind: 'runner-add' })} onCreate={() => setView({ kind: 'create' })}
                onEdit={payload => setView({ kind: 'runner-edit', payload })} />;
            case 'renderer': return <RendererGroup key={g.kind} group={g} filtering={grouped.filtering} onShowCell={onShowCell} slot={catalogs.renderers} />;
            case 'handler': return <CapabilityGroup key={g.kind} kind={g.kind} title="Handlers" subtitle={GROUP_SUBTITLES.handler}
                shown={g.user.length + g.builtin.length} total={g.total} filtering={grouped.filtering}
                loading={!catalogs.handlers.data && !catalogs.handlers.error} error={catalogs.handlers.error} onRetry={catalogs.handlers.retry}
                empty="No handlers yet. Create one, or add a .handler from any cell’s Files panel.">
                {catalogs.handlers.data && <HandlerManagement filtering={grouped.filtering} focus={focus} onShowCell={onShowCell}
                    onRevealed={seq => setFocus(current => (current?.seq === seq ? null : current))}
                    handlers={g.user.map(entry => entry.item)} builtins={g.builtin.map(entry => entry.item)}
                    onEdit={(handler, saved) => setView({ kind: 'handler-edit', handler, saved })} />}
            </CapabilityGroup>;
        }
    };

    return <>
        <ScrollArea className="min-h-0 flex-1" hidden={!listShown}
            onWheel={e => { if (isFocusWheel(e)) e.stopPropagation(); }}>
            <div data-capabilities-list="" className="flex flex-col gap-3.5 px-3.5 pt-3 pb-5 text-[0.8125rem]">
                <CapabilitiesAbout />
                <ProblemsStrip problems={catalogs.problems.data ?? []} cellName={cellName} onShowCell={onShowCell} />
                <CapabilitiesToolbar query={query} onQuery={setQuery} kinds={kinds} onKinds={setKinds} counts={grouped.counts}
                    searchRef={searchRef} onAddFromCatalog={() => setView({ kind: 'catalog' })} onCreate={() => setView({ kind: 'create' })} />
                {notice && <p role="status" className="px-0.5 text-[0.75rem] text-ink-2">{notice}</p>}
                {loading
                    ? <p className="px-0.5 text-[0.78125rem] text-ink-3">Loading capabilities…</p>
                    : grouped.filtering && grouped.matched === 0
                        ? <div data-capabilities-no-match="" className="flex flex-col items-start gap-2 px-0.5">
                            <p className="text-[0.78125rem] text-ink-2">Nothing matches ‘{query.trim()}’.</p>
                            <button type="button" className={btn} onClick={() => setView({ kind: 'create', description: query.trim() })}>Create a capability for it…</button>
                        </div>
                        : grouped.groups.map(group)}
            </div>
        </ScrollArea>
        {view.kind === 'runner-add' && <RunnerAddView key={`${view.generatorId ?? ''}`} generatorId={view.generatorId} description={view.description}
            generators={generators?.runnerGenerators ?? null} onBack={back} onSaved={done} />}
        {view.kind === 'runner-edit' && <RunnerEditView payload={view.payload} onBack={back} onSaved={done} />}
        {view.kind === 'handler-edit' && <HandlerEditView handler={view.handler} onBack={back}
            onSaved={text => { view.saved(text); back(); }} />}
        {view.kind === 'catalog' && <CatalogAddView onBack={back}
            onAdded={(cellId, name) => { done(`‘${name}’ is on Capabilities.`); onShowCell(cellId); }} />}
        {view.kind === 'create' && <SubView title="Create a capability" onBack={back}>
            <Suspense fallback={<p className="text-[0.78125rem] text-ink-3">Loading…</p>}>
                <CreateQuestionnaire initialDescription={view.description} prefs={renderers?.prefs ?? {}}
                    catalogs={fitCatalogs({
                        generators: generators?.generators, runnerGenerators: generators?.runnerGenerators,
                        renderers: renderers?.renderers, handlers: catalogs.handlers.data ?? undefined,
                    }, cellName)}
                    onCancel={back} onExit={exitCreate} />
            </Suspense>
        </SubView>}
    </>;
}
