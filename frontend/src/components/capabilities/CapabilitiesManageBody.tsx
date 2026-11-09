import { lazy, Suspense, useEffect, useRef, useState, type RefObject } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelReadOnly } from '@/contexts/ChannelReadOnlyContext';
import type { CapabilitiesManageRequest } from '@/stores/collimate/selectionSlice';
import { capabilityEntries, catalogInCell, groupCapabilities, inCell, type AnyCapabilityGroup } from '@/lib/capabilityList';
import { fitCatalogs } from '@/lib/capabilityFit';
import type { HandlerInfo } from '@/lib/definitionApi';
import { revealHandler } from '@/lib/revealFile';
import { fetchRunner, type RunnerEditorPayload } from '@/lib/runnerApi';
import { keyboardIsFree } from '@/lib/selectionFocus';
import { findCapabilitiesStack, type CapabilityKind } from '@/lib/types';
import { isFocusWheel } from '@/lib/wheelReplay';
import { GeneratorList } from '@/components/cellEditor/GeneratorRows';
import { LibraryList } from './LibraryList';
import { HandlerEditView } from '@/components/cellEditor/HandlerEditView';
import { HandlerManagement, type HandlerFocus } from '@/components/cellEditor/HandlerManagement';
import { ScrollArea } from '@/components/ui/scroll-area';
import { btn } from '@/components/ui/formStyles';
import { CapabilitiesToolbar } from './CapabilitiesToolbar';
import { CapabilityGroup } from './CapabilityGroup';
import { CatalogAddView } from './CatalogAddView';
import { CellCopyView } from './CellCopyView';
import { CellPublishView } from './CellPublishView';
import { CellStrip } from './CellStrip';
import { ReplaceView } from './ReplaceView';
import { UpdatesStrip, UpstreamLine } from './UpdatesStrip';
import type { CreateExit } from './CreateQuestionnaire';
import { ProblemsStrip } from './ProblemsStrip';
import { RendererGroup } from './RendererGroup';
import { RunnerAddView, RunnerEditView, RunnersGroup } from './RunnersGroup';
import { SubView } from './SubView';
import { useCapabilityCatalogs } from './useCapabilityCatalogs';

const CreateQuestionnaire = lazy(() => import('./CreateQuestionnaire'));

/** One Capabilities cell: the body lists only what it holds. */
export interface ManageScope {
    cellId: string;
}

export type CapabilitiesManageBodyProps =
    /** The master face (the Capabilities stack's slot 0): every capability
     *  of the channel, the store's requests, the reveals into other cells. */
    | {
        scope?: undefined;
        /** Fronted on the selected stack: the search takes the keyboard. */
        active: boolean;
        /** The card root, focused under an editor. */
        rootRef: RefObject<HTMLDivElement | null>;
        /** Bring a Capabilities cell to the front of the stack. */
        onShowCell: (cellId: string) => void;
    }
    /** A Capabilities cell's Manage tab. */
    | { scope: ManageScope };

/** What replaces the list (component state: it resets when the stack is
 *  hidden or the channel switches). */
type View =
    | { kind: 'list' }
    | { kind: 'runner-add'; generatorId?: string; description?: string }
    | { kind: 'runner-edit'; payload: RunnerEditorPayload }
    | { kind: 'handler-edit'; handler: HandlerInfo; saved: (notice: string) => void }
    | { kind: 'create'; description?: string }
    | { kind: 'catalog' }
    | { kind: 'copy' }
    | { kind: 'publish' }
    | { kind: 'replace'; cellId: string };

const LIST: View = { kind: 'list' };

/** Frames the master face waits for the keyboard to come free when fronted
 *  (the selection hand-off releases another cell's keys one frame on). */
const FOCUS_RETRY_FRAMES = 2;

const GROUP_SUBTITLES: Record<CapabilityKind, string> = {
    generator: 'Make files.',
    runner: 'Start a generator with saved settings.',
    renderer: 'Show files.',
    handler: 'Check files and teach agents.',
    library: 'Code generators share, named under shared: in a cell’s cell.yaml.',
};

/** What each group says when it holds nothing: on the master face, in the
 *  channel; in a cell's Manage tab, in that cell. */
const EMPTY = {
    channel: {
        generator: 'No generators yet. Create one, or add a .generator from any cell’s Files panel.',
        renderer: 'No renderers yet. Create one, or add a .renderer from any cell’s Files panel.',
        handler: 'No handlers yet. Create one, or add a .handler from any cell’s Files panel.',
        library: 'No libraries yet. Add one from the catalog, or add a .library from any cell’s Files panel.',
    },
    cell: {
        generator: 'No generators in this cell.',
        renderer: 'No renderers in this cell.',
        handler: 'No handlers in this cell.',
        library: 'No libraries in this cell.',
    },
} as const;

/**
 * The Capabilities manage view, one UI in two places. The master face
 * (CapabilitiesManageCard, slot 0 of the Capabilities stack) lists every
 * generator, runner, renderer and handler of the channel: it applies the
 * store's one-shot `capabilitiesManageRequest` (the only consumer), focuses
 * its search when fronted on the selected stack, offers Add from
 * catalog…, and reveals other cells (`onShowCell`).
 *
 * With a `scope` it is a Capabilities cell's Manage tab: the same catalogs
 * narrowed to the records whose `cell_id` is that cell (problems too), no
 * Add from catalog… and no Open cell (everything shown is here;
 * Open file opens a tab beside it), the Built-in disclosures open (a
 * built-in card holds nothing else), and a runner added from it (the
 * Runners group's Add runner, a generator's Add runner) is kept in this
 * cell with no 'Keep it in' step; a built-in card keeps no runner, so it
 * offers no Add runner. Create… stays, weighing the whole channel: a fit
 * held by another cell is revealed on the master face (revealHandler). It
 * never reads the store's request, never takes the keyboard on its own and
 * owns no keys but the Esc that clears its search's query (the toolbar's):
 * typing in its search is the ordinary typing guard's, and every chord
 * reaches the scene.
 *
 * In an archived channel (ChannelReadOnlyContext) it only reads: the search
 * and the kind chips, Copy to channel and Publish to catalog (both read
 * this channel and write elsewhere, D9). Create…, Add from catalog…, the
 * updates' Review and replace… and every group's controls are off, and a
 * request for a runner, Create or the catalog lands on the list.
 */
export function CapabilitiesManageBody(props: CapabilitiesManageBodyProps) {
    const scope = props.scope ?? null;
    const master = props.scope ? null : props;
    const readOnly = useChannelReadOnly();
    const onShowCell = master?.onShowCell;
    const cells = useCollimateStore(s => findCapabilitiesStack(s.activeChannel)?.stack.cells);
    const cellName = (cellId: string | undefined) => cells?.find(cell => cell.id === cellId)?.name ?? '';
    const builtinCell = !!scope && !!cells?.find(cell => cell.id === scope.cellId)?.builtin;
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

    // A request from elsewhere (the store's one-shot capabilitiesManageRequest),
    // the master face's alone: applied once, then cleared by its seq.
    const request = useCollimateStore(s => (master ? s.capabilitiesManageRequest : null));
    const [applied, setApplied] = useState<number | null>(null);
    if (request && request.seq !== applied) {
        setApplied(request.seq);
        apply(request);
    }
    function apply(next: CapabilitiesManageRequest) {
        setNotice('');
        if (readOnly && (next.runner || next.create || next.addFromCatalog)) {
            setView(LIST);
            if (next.kind) setKinds([next.kind]);
            if (next.query !== undefined) setQuery(next.query);
            return;
        }
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

    // The master face, fronted on the selected stack with the keyboard
    // free: the search (or, under an editor, the card) takes it. Keys
    // another cell still holds are released a frame after the selection
    // moved (useSelectionFocus), so the card looks again on the next two
    // frames.
    const listShown = view.kind === 'list';
    const active = master?.active ?? false;
    const rootRef = master?.rootRef;
    useEffect(() => {
        if (!active) return;
        let raf = 0;
        let retries = FOCUS_RETRY_FRAMES;
        const take = () => {
            if (keyboardIsFree(document)) (listShown ? searchRef.current : rootRef?.current)?.focus({ preventScroll: true });
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
    const data = {
        generators: generators?.generators,
        runners: catalogs.runners.data ?? undefined,
        renderers: renderers?.renderers,
        handlers: catalogs.handlers.data ?? undefined,
        libraries: catalogs.libraries.data ?? undefined,
    };
    const entries = capabilityEntries(scope ? catalogInCell(data, scope.cellId) : data, cellName);
    const problems = catalogs.problems.data ?? [];
    const grouped = groupCapabilities(entries, { query, kinds });
    const loading = !generators && !catalogs.runners.data && !renderers && !catalogs.handlers.data
        && !catalogs.generators.error && !catalogs.runners.error && !catalogs.renderers.error && !catalogs.handlers.error;
    const schemas = Object.fromEntries((generators?.runnerGenerators ?? []).map(schema => [schema.id, schema]));
    const empty = EMPTY[scope ? 'cell' : 'channel'];
    const done = (text: string) => { setView(LIST); setNotice(text); };
    const addRunner = builtinCell ? undefined : () => setView({ kind: 'runner-add' });

    // Create… weighs the whole channel, so its fit can be a handler another
    // cell holds: a cell's Manage tab hands that one to the master face
    // (revealHandler fronts it) and keeps only its own.
    const exitCreate = (exit: CreateExit) => {
        if (exit.kind === 'handler' && scope && exit.cellId !== scope.cellId) {
            revealHandler(exit.cellId, exit.folder, exit.step);
            setView(LIST);
        } else if (exit.kind === 'handler') focusHandler(exit);
        else if (exit.kind === 'runner') setView({ kind: 'runner-add', generatorId: exit.generatorId, description: exit.description });
        else done(exit.text);
    };

    const group = (g: AnyCapabilityGroup) => {
        if (grouped.filtering && !g.user.length && !g.builtin.length) return null;
        switch (g.kind) {
            case 'generator': return <CapabilityGroup key={g.kind} kind={g.kind} title="Generators" subtitle={GROUP_SUBTITLES.generator}
                shown={g.user.length} total={g.total} filtering={grouped.filtering}
                loading={!generators && !catalogs.generators.error} error={catalogs.generators.error} onRetry={catalogs.generators.retry}
                empty={empty.generator}>
                {g.user.length > 0 && <GeneratorList generators={g.user.map(entry => entry.item)} schemas={schemas} onShowCell={onShowCell}
                    onAddRunner={generator => setView({ kind: 'runner-add', generatorId: generator.name })} />}
            </CapabilityGroup>;
            case 'runner': return <RunnersGroup key={g.kind} group={g} filtering={grouped.filtering} onShowCell={onShowCell}
                slot={catalogs.runners} generators={generators?.generators ?? null} cellName={cellName}
                onAdd={addRunner} onCreate={() => setView({ kind: 'create' })}
                onEdit={payload => setView({ kind: 'runner-edit', payload })} />;
            case 'renderer': return <RendererGroup key={g.kind} group={g} filtering={grouped.filtering} onShowCell={onShowCell} slot={catalogs.renderers}
                builtinsOpen={!!scope} empty={empty.renderer} />;
            case 'handler': return <CapabilityGroup key={g.kind} kind={g.kind} title="Handlers" subtitle={GROUP_SUBTITLES.handler}
                shown={g.user.length + g.builtin.length} total={g.total} filtering={grouped.filtering}
                loading={!catalogs.handlers.data && !catalogs.handlers.error} error={catalogs.handlers.error} onRetry={catalogs.handlers.retry}
                empty={empty.handler}>
                {catalogs.handlers.data && <HandlerManagement filtering={grouped.filtering} focus={focus} onShowCell={onShowCell}
                    builtinsOpen={!!scope}
                    onRevealed={seq => setFocus(current => (current?.seq === seq ? null : current))}
                    handlers={g.user.map(entry => entry.item)} builtins={g.builtin.map(entry => entry.item)}
                    onEdit={(handler, saved) => setView({ kind: 'handler-edit', handler, saved })} />}
            </CapabilityGroup>;
            case 'library': return <CapabilityGroup key={g.kind} kind={g.kind} title="Libraries" subtitle={GROUP_SUBTITLES.library}
                shown={g.user.length + g.builtin.length} total={g.total} filtering={grouped.filtering}
                loading={!catalogs.libraries.data && !catalogs.libraries.error} error={catalogs.libraries.error} onRetry={catalogs.libraries.retry}
                empty={empty.library}>
                <LibraryList libraries={g.user.map(entry => entry.item)} app={g.builtin.map(entry => entry.item)}
                    cellName={cellName} onShowCell={onShowCell} />
            </CapabilityGroup>;
        }
    };

    return <>
        <ScrollArea className="min-h-0 flex-1" hidden={!listShown}
            // The master face keeps a focusing wheel for its list; in a
            // cell the wheel meets the card as over any tab.
            onWheel={master ? (e => { if (isFocusWheel(e)) e.stopPropagation(); }) : undefined}>
            <div data-capabilities-list="" className="flex flex-col gap-3.5 px-3.5 pt-3 pb-5 text-[0.8125rem]">
                {scope && !builtinCell && <CellStrip onCopy={() => setView({ kind: 'copy' })} onPublish={() => setView({ kind: 'publish' })}>
                    {!readOnly && catalogs.updates.data && <UpstreamLine follow={catalogs.updates.data.find(row => row.cell_id === scope.cellId)}
                        onReview={() => setView({ kind: 'replace', cellId: scope.cellId })} />}
                </CellStrip>}
                {!scope && !readOnly && <UpdatesStrip updates={catalogs.updates.data ?? []} cellName={cellName}
                    onReview={cellId => setView({ kind: 'replace', cellId })} />}
                <ProblemsStrip problems={scope ? inCell(problems, scope.cellId) : problems} cellName={cellName} onShowCell={onShowCell} />
                <CapabilitiesToolbar query={query} onQuery={setQuery} kinds={kinds} onKinds={setKinds} counts={grouped.counts}
                    searchRef={searchRef} onAddFromCatalog={scope || readOnly ? undefined : () => setView({ kind: 'catalog' })}
                    onCreate={readOnly ? undefined : () => setView({ kind: 'create' })} />
                {notice && <p role="status" className="px-0.5 text-[0.75rem] text-ink-2">{notice}</p>}
                {loading
                    ? <p className="px-0.5 text-[0.78125rem] text-ink-3">Loading capabilities…</p>
                    : grouped.filtering && grouped.matched === 0
                        ? <div data-capabilities-no-match="" className="flex flex-col items-start gap-2 px-0.5">
                            <p className="text-[0.78125rem] text-ink-2">Nothing matches ‘{query.trim()}’.</p>
                            {!readOnly && <button type="button" className={btn} onClick={() => setView({ kind: 'create', description: query.trim() })}>Create a capability for it…</button>}
                        </div>
                        // A read-only channel's groups show, their controls off.
                        : <fieldset disabled={readOnly} data-capabilities-read-only={readOnly || undefined} className="contents">
                            {grouped.groups.map(group)}
                        </fieldset>}
            </div>
        </ScrollArea>
        {view.kind === 'runner-add' && <RunnerAddView key={`${view.generatorId ?? ''}`} generatorId={view.generatorId} description={view.description}
            generators={generators?.runnerGenerators ?? null} cellId={builtinCell ? undefined : scope?.cellId} onBack={back} onSaved={done} />}
        {view.kind === 'runner-edit' && <RunnerEditView payload={view.payload} onBack={back} onSaved={done} />}
        {view.kind === 'handler-edit' && <HandlerEditView handler={view.handler} onBack={back}
            onSaved={text => { view.saved(text); back(); }} />}
        {view.kind === 'catalog' && master && <CatalogAddView onBack={back}
            onAdded={(cellId, name) => { done(`‘${name}’ is on Capabilities.`); master.onShowCell(cellId); }} />}
        {view.kind === 'copy' && scope && <CellCopyView cellId={scope.cellId} cellName={cellName(scope.cellId)}
            onBack={back} onDone={done} />}
        {view.kind === 'replace' && <ReplaceView cellId={view.cellId} cellName={cellName(view.cellId)}
            onBack={back} onDone={done} />}
        {view.kind === 'publish' && scope && <CellPublishView cellId={scope.cellId} cellName={cellName(scope.cellId)}
            onBack={back} onDone={done} />}
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
