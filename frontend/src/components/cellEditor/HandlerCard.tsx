import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { HandlerStep } from '@/stores/collimate/selectionSlice';
import {
    acceptHandler, addBuiltinExample, deleteBuiltinExample, inspectHandler, patchBuiltinExample, revertBuiltinSkills,
    type BuildInspection, type ExampleDeclaration, type HandlerInfo,
} from '@/lib/definitionApi';
import { RENDERER_PREFS_CHANGED_EVENT } from '@/lib/api';
import { spawnHandlerBuild } from '@/lib/api/capabilitiesStack';
import { openSpawnedCell } from '@/lib/capabilitySpawn';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { flushFileDocuments } from '@/lib/fileDocuments';
import { addUserExample, compareExamples, matchCount, patchUserExample, removeUserExample, withoutHandler, type ExampleVerdict } from '@/lib/handlerExamples';
import {
    ACTION_LABELS, clashLine, handlerState, hint, nextLabel, short, VALIDATE_NEEDS_BUILD,
    type HandlerAction, type HandlerTone,
} from '@/lib/handlerStatus';
import { revealCellFile } from '@/lib/revealFile';
import { BuiltinChip } from '@/components/BuiltinChip';
import { btn, btnDanger, btnPrimary, card, chip, confirmStrip } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { HandlerExamples } from './HandlerExamples';
import { AddExampleForm, ReviewStep, StepFrame, WorkStackStep } from './handlerSteps';

/** The status dot of each tone. */
const TONE: Record<HandlerTone, string> = {
    green: 'bg-green',
    'green-waiting': 'bg-green ring-2 ring-brass ring-offset-1 ring-offset-sheet',
    brass: 'bg-brass',
    red: 'bg-red',
    muted: 'bg-ink-3',
};

/** What the app's handler chip says on hover. */
const BUILTIN_TITLE = 'One of the app’s handlers: its checks are the app’s and it cannot be edited or deleted. Add examples and regenerate its skills here.';

/** Whether a failed request was refused because the handler changed. */
const isConflict = (e: unknown) => (e as { response?: { status?: number } }).response?.status === 409;

type Step =
    | { kind: 'validate' }
    | { kind: 'review'; inspection: BuildInspection }
    | { kind: 'addExample' }
    | { kind: 'workStack' };

/** One file type the handler claims, as a chip with what it means. */
function TypeChip({ type }: { type: HandlerInfo['types'][number] }) {
    let label = type.ext;
    const titles = [`Files ending ${type.ext}`];
    if (type.render_as) {
        label += ` → shown as ${type.render_as}`;
        titles.push(`Opened with the renderer for ${type.render_as} files; saved under its own name`);
    }
    return <span className={cn(chip, 'font-mono')} title={titles.length > 1 ? titles.slice(1).join('. ') : titles[0]}>{label}</span>;
}

/** The status dot: its tone, and the label as its name. */
function StatusDot({ handler }: { handler: HandlerInfo }) {
    const state = handlerState(handler);
    return <span role="img" aria-label={state.label} title={state.label} data-status={state.key}
        className={cn('size-2 flex-none rounded-full', TONE[state.tone])} />;
}

/**
 * One handler of the channel, managed in place in the Capabilities manage
 * view's Handlers group: what it is (name, id, description, the types it
 * claims), where it stands in plain words (lib/handlerStatus) with the one
 * action that moves it forward, its examples, and a row of actions whose
 * steps open under it one at a time. Every action acts on the cell that
 * holds the handler (`handler.cell_id`).
 *
 * Build… opens a work stack beside Capabilities with a copy of the handler
 * and its brief; nothing runs here. Its result comes back through that
 * stack's Files panel and waits on this card for Review and accept. Edit
 * replaces the manage view's list (`onEdit`); Delete confirms in a strip
 * and removes the handler's folder from its cell.
 *
 * A built-in (the Definitions card) has no Edit or Delete: its checks are
 * the app's; the channel adds examples and regenerates its skills in a
 * work stack.
 */
export function HandlerCard({ handler, collapsible, focus, onRevealed, onEdit, onShowCell }: {
    handler: HandlerInfo;
    /** The card starts folded to its name and status (a long list); a
     *  click on its head unfolds it. */
    collapsible: boolean;
    /** A request's handler focus (the store's capabilitiesManageRequest):
     *  each new `seq` unfolds the card, scrolls to it, flashes it and opens
     *  `step`. */
    focus?: { seq: number; step?: HandlerStep };
    /** The card has shown focus `seq`: the holder stops passing it, so a
     *  remount (a search hid the card, then showed it again) never shows it
     *  twice. */
    onRevealed: (seq: number) => void;
    onEdit: (handler: HandlerInfo, saved: (notice: string) => void) => void;
    /** Bring the handler's cell to the front of Capabilities. */
    onShowCell: (cellId: string) => void;
}) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const cellId = handler.cell_id;
    const cellFiles = useCollimateStore(s => s.locateCell(cellId)?.cell.files);
    const [open, setOpen] = useState(!collapsible);
    const [step, setStep] = useState<Step | null>(null);
    const [confirm, setConfirm] = useState<'delete' | 'revert' | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [verdicts, setVerdicts] = useState<Map<string, ExampleVerdict[]> | null>(null);
    const [compileError, setCompileError] = useState('');
    const [examplesOpen, setExamplesOpen] = useState(false);
    const [flash, setFlash] = useState(false);
    const root = useRef<HTMLElement>(null);

    const state = handlerState(handler);
    const clash = clashLine(handler);
    const next = nextLabel(handler);
    const waiting = state.key === 'review-first' || state.key === 'review-next' || state.key === 'regen-waiting';
    const canRevert = handler.builtin && (!handler.uses_app_skills || handler.has_derived);

    const work = async (fallback: string, fn: () => Promise<void>) => {
        setBusy(true); setError(''); setNotice('');
        try { await fn(); }
        catch (e) { setError(e instanceof Error ? e.message : fallback); }
        finally { setBusy(false); }
    };
    /** The catalog changed: the manage view reloads its handlers (and the
     *  renderer registry its renderers) on this event. */
    const changed = () => window.dispatchEvent(new Event(RENDERER_PREFS_CHANGED_EVENT));
    /** Save every open editor and draft first: the inspection reads the files. */
    const inspect = async () => {
        await flushCellSaves(cellId); await flushFileDocuments();
        const inspection = await inspectHandler(channel, cellId, handler.folder);
        setVerdicts(compareExamples(inspection));
        return inspection;
    };

    const validate = () => {
        setStep({ kind: 'validate' }); setCompileError('');
        return work('Could not validate the handler', async () => {
            const inspection = await inspect();
            // A built-in's checks are the app's: there is no build to compile.
            if (!handler.builtin && inspection.compile?.valid === false) {
                setCompileError(`The checks of build ${short(handler.built_hash)} do not compile: ${inspection.compile.error || 'no reason given'}`);
            }
            setExamplesOpen(true);
        });
    };
    const review = () => work('Could not review the build', async () => {
        setStep({ kind: 'review', inspection: await inspect() });
    });
    const accept = (inspection: BuildInspection) => work('Could not accept the build', async () => {
        try {
            await acceptHandler(channel, cellId, handler.folder, inspection.content_hash);
        } catch (e) {
            if (isConflict(e)) throw new Error('The handler changed while you reviewed it. Review again.', { cause: e });
            throw e;
        }
        setStep(null);
        setNotice(handler.builtin
            ? `Skills ${short(inspection.derived_hash)} are in use.`
            : `Build ${short(inspection.derived_hash)} is in use.`);
        changed();
    });
    const revert = () => work('Could not restore the app’s skills', async () => {
        await revertBuiltinSkills(channel, handler.folder);
        setConfirm(null); setStep(null); setNotice('Agents get the app’s skills again.');
        changed();
    });
    /** Save what is open, then open the work stack and its brief. */
    const openWorkStack = () => work('Could not open the work stack', async () => {
        await flushCellSaves(cellId); await flushFileDocuments();
        openSpawnedCell(await spawnHandlerBuild(channel, { cell_id: cellId, folder: handler.folder }));
        setStep(null);
        setNotice(handler.builtin
            ? `Work stack ‘Regenerate ${handler.name} skills’ is open. Use its skills from its Files panel, then review them here.`
            : `Work stack ‘Build ${handler.name}’ is open. Add ${handler.folder}/ back from its Files panel, then review the build here.`);
    });
    const addExample = (sourceCellId: string, paths: string[], declaration: ExampleDeclaration) => work('Could not add the example', async () => {
        if (handler.builtin) {
            await flushCellSaves(sourceCellId); await flushFileDocuments();
            await addBuiltinExample(channel, handler.folder, { source_cell_id: sourceCellId, paths, ...declaration });
        } else {
            await addUserExample(handler, sourceCellId, paths, declaration);
        }
        setStep(null); setExamplesOpen(true); setNotice('Example added.');
        changed();
    });
    const saveExample = (folder: string, fields: ExampleDeclaration) => work('Could not save the example', async () => {
        if (handler.builtin) await patchBuiltinExample(channel, handler.folder, folder, fields);
        else await patchUserExample(handler, folder, fields);
        setNotice('Example saved.');
        changed();
    });
    const removeExample = (folder: string) => work('Could not remove the example', async () => {
        if (handler.builtin) await deleteBuiltinExample(channel, handler.folder, folder);
        else await removeUserExample(handler, folder);
        setNotice('Example removed.');
        changed();
    });
    const remove = () => work('Could not delete the handler', async () => {
        await flushCellSaves(cellId);
        const store = useCollimateStore.getState();
        const files = store.locateCell(cellId)?.cell.files;
        if (!files) throw new Error('The handler cell is unavailable.');
        if (!await store.applyCellFiles(cellId, withoutHandler(files, handler))) throw new Error('Could not delete the handler.');
        setConfirm(null);
        changed();
    });

    // A reveal: unfold and flash the card and open the step it asks for,
    // then scroll to it; Validate and Review start their inspection. The
    // step is recorded with the seq, so the holder may drop the focus once
    // it is shown (onRevealed) without cancelling the inspection.
    const [revealed, setRevealed] = useState<{ seq: number; step?: HandlerStep } | undefined>(undefined);
    if (focus && focus.seq !== revealed?.seq) {
        setRevealed({ seq: focus.seq, step: focus.step }); setOpen(true); setFlash(true);
        if (focus.step === 'addExample') setStep({ kind: 'addExample' });
    }
    const inspectOnReveal = useEffectEvent(() => {
        if (revealed?.step === 'validate') void validate();
        else if (revealed?.step === 'review') void review();
    });
    const shown = useEffectEvent((seq: number) => onRevealed(seq));
    useEffect(() => {
        if (revealed === undefined) return;
        root.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
        const inspecting = setTimeout(() => inspectOnReveal(), 0);
        const unflash = setTimeout(() => setFlash(false), 1200);
        shown(revealed.seq);
        return () => { clearTimeout(inspecting); clearTimeout(unflash); };
    }, [revealed]);

    const action = (id: HandlerAction, onClick: () => void, options: { disabled?: string; danger?: boolean } = {}): ReactNode => {
        const primary = state.next === id;
        const title = options.disabled ?? hint(id, handler);
        return <button key={id} type="button" data-action={id}
            className={options.danger ? btnDanger : primary ? btnPrimary : btn}
            disabled={busy || !!options.disabled} title={title} aria-description={title}
            onClick={onClick}>{ACTION_LABELS[id]}</button>;
    };
    const actions: ReactNode[] = handler.builtin
        ? [
            action('validate', () => void validate()),
            action('regenerate', () => { setError(''); setStep({ kind: 'workStack' }); }),
            waiting && action('review', () => void review()),
            canRevert && action('revert', () => setConfirm('revert')),
            action('addExample', () => { setError(''); setStep({ kind: 'addExample' }); }),
            action('openCard', () => onShowCell(cellId)),
        ]
        : [
            action('validate', () => void validate(), { disabled: handler.has_derived ? undefined : VALIDATE_NEEDS_BUILD }),
            action('build', () => { setError(''); setStep({ kind: 'workStack' }); }),
            waiting && action('review', () => void review()),
            action('addExample', () => { setError(''); setStep({ kind: 'addExample' }); }),
            action('edit', () => onEdit(handler, setNotice)),
            action('openFile', () => revealCellFile(cellId, handler.definition_path)),
            action('openCell', () => onShowCell(cellId)),
            action('delete', () => setConfirm('delete'), { danger: true }),
        ];

    const closeStep = () => { setStep(null); setError(''); };
    const stepNode = (): ReactNode => {
        if (!step) return null;
        if (step.kind === 'validate') {
            const tally = verdicts ? matchCount(verdicts) : null;
            return <StepFrame title={`Validate ‘${handler.name}’`} onClose={closeStep}>
                {busy && <p className="text-[0.78125rem] text-ink-2">Running {handler.examples.length} example{handler.examples.length === 1 ? '' : 's'}…</p>}
                {!busy && compileError && <p role="alert" className="rounded-md bg-red-soft px-2 py-1.5 text-[0.78125rem] text-red">{compileError}</p>}
                {!busy && !compileError && tally && <p className="text-[0.78125rem] text-ink">
                    {tally.total ? `${tally.matched} of ${tally.total} examples match what they expect. Their results are on the examples above.` : 'No examples to run yet. Add one first.'}
                </p>}
            </StepFrame>;
        }
        if (step.kind === 'review') return <ReviewStep handler={handler} inspection={step.inspection} busy={busy}
            onAccept={() => void accept(step.inspection)} onRevert={canRevert ? () => setConfirm('revert') : undefined} onClose={closeStep} />;
        if (step.kind === 'addExample') return <AddExampleForm handler={handler} busy={busy} onAdd={(source, paths, declaration) => void addExample(source, paths, declaration)} onClose={closeStep} />;
        return <WorkStackStep handler={handler} busy={busy} onOpen={() => void openWorkStack()} onClose={closeStep} />;
    };

    const identity = <>
        <span className="min-w-0 truncate text-[0.8125rem] font-semibold text-ink">{handler.name}</span>
        {handler.builtin && <BuiltinChip title={BUILTIN_TITLE} />}
        <span className="ml-auto min-w-0 truncate font-mono text-[0.75rem] text-ink-3">{handler.id}</span>
        <StatusDot handler={handler} />
    </>;

    return <article ref={root} data-handler-card={handler.folder} aria-label={`${handler.name} handler`}
        className={cn(card, 'overflow-hidden transition-colors', flash && 'border-teal')}>
        {collapsible
            ? <button type="button" aria-expanded={open} className="flex w-full items-center gap-2 px-3 pt-3 text-left" onClick={() => setOpen(value => !value)}>
                <ChevronRight className={cn('size-3.5 flex-none text-ink-3 transition-transform', open && 'rotate-90')} aria-hidden />
                {identity}
            </button>
            : <div className="flex items-center gap-2 px-3 pt-3">{identity}</div>}
        {!open && <p className="px-3 pb-3 pt-1 text-[0.75rem] text-ink-2">{state.label}</p>}
        {open && <div className="flex flex-col">
            <div className="flex flex-col gap-1.5 px-3 pb-3 pt-1">
                {handler.description && <p className="text-[0.78125rem] leading-[1.45] text-ink-2">{handler.description}</p>}
                <div className="flex flex-wrap items-center gap-1">
                    <span className="mr-1 text-[0.75rem] text-ink-3">Handles</span>
                    {handler.types.map(type => <TypeChip key={type.ext} type={type} />)}
                </div>
            </div>
            <div data-handler-status="" className="flex flex-col gap-1 border-t border-line px-3 py-2.5">
                <div className="flex items-center gap-2">
                    <StatusDot handler={handler} />
                    <span className="text-[0.78125rem] font-medium text-ink">{state.label}</span>
                </div>
                <p className="pl-4 text-[0.78125rem] leading-[1.45] text-ink-2">{state.detail}</p>
                {clash && <p className="pl-4 text-[0.78125rem] leading-[1.45] text-red">{clash}</p>}
                <p className="pl-4 text-[0.75rem] text-ink-3">Next: <span className="text-ink-2">{next ?? '—'}</span></p>
            </div>
            <div className="border-t border-line px-3 py-2.5">
                <HandlerExamples handler={handler} cellFiles={(cellFiles ?? []).map(file => file.name)} verdicts={verdicts}
                    open={examplesOpen} onToggle={() => setExamplesOpen(value => !value)} busy={busy}
                    onOpen={path => revealCellFile(cellId, path)}
                    onSave={(folder, fields) => void saveExample(folder, fields)}
                    onRemove={folder => void removeExample(folder)} />
            </div>
            <div className="flex flex-col gap-2 border-t border-line px-3 py-2.5">
                {notice && <p role="status" className="text-[0.75rem] text-ink-2">{notice}</p>}
                {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
                <div role="group" aria-label={`${handler.name} actions`} className="flex flex-wrap gap-1.5">{actions}</div>
                {stepNode()}
            </div>
        </div>}
        {confirm === 'delete' && <div className={confirmStrip}>
            <span className="min-w-0 flex-1">Delete ‘{handler.name}’? Its folder leaves its cell and its {handler.types.map(type => type.ext).join(', ')} files are no longer checked.</span>
            <button type="button" className={btnDanger} disabled={busy} onClick={() => void remove()}>Delete</button>
            <button type="button" className={btn} onClick={() => setConfirm(null)}>Keep</button>
        </div>}
        {confirm === 'revert' && <div className={confirmStrip}>
            <span className="min-w-0 flex-1">Drop this channel’s regenerated skills of ‘{handler.name}’? Agents get the app’s skills again.</span>
            <button type="button" className={btnDanger} disabled={busy} onClick={() => void revert()}>Use the app’s skills</button>
            <button type="button" className={btn} onClick={() => setConfirm(null)}>Keep</button>
        </div>}
    </article>;
}
