import { useState, type ReactNode } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { spawnCapabilityCreate } from '@/lib/api/capabilitiesStack';
import {
    assessFit, FIT_PATHS, FIT_QUESTIONS, fitText, HOW_LABELS, knownTypes, LOOK_LABELS, normalizeExt, readyToDescribe, stepAnswers,
    type Fit, type FitAnswers, type FitCatalogs, type FitPath,
} from '@/lib/capabilityFit';
import { openSpawnedCell } from '@/lib/capabilitySpawn';
import { CAPABILITY_KINDS } from '@/lib/capabilityList';
import type { CapabilityKind } from '@/lib/types';
import { btn, btnPrimary, card, formInput, formLabel, formSelect, formTextarea, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { makeDefaultRenderer } from './manageActions';

/** How the questionnaire ended without a spawn of its own choosing. */
export type CreateExit =
    /** Show a handler that already does the job (and open a step there). */
    | { kind: 'handler'; cellId: string; folder: string; step?: 'addExample' }
    /** A runner is enough: open the runner editor for this generator. */
    | { kind: 'runner'; generatorId: string; description: string }
    /** Done: back to the list with this notice. */
    | { kind: 'notice'; text: string };

const STEP_NAMES = ['Choose', 'Details', 'Describe', 'Confirm'] as const;

const KIND_LINES: Record<CapabilityKind, string> = {
    generator: 'A generator: a new way of making files',
    runner: 'A runner: saved settings that start a generator',
    renderer: 'A renderer: a view for the files',
    handler: 'A handler: checks and agent skills for the file type',
};

const CHOICE = 'flex items-start gap-2 rounded-md border px-2.5 py-2 text-left transition-colors';
const choiceTone = (on: boolean) => (on ? 'border-teal bg-teal-soft' : 'border-line-2 hover:bg-sheet-2');

function Radio({ name, checked, onChange, children }: { name: string; checked: boolean; onChange: () => void; children: ReactNode }) {
    return <label className={cn(CHOICE, choiceTone(checked), 'cursor-pointer')}>
        <input type="radio" name={name} className="mt-0.5 accent-teal" checked={checked} onChange={onChange} />
        <span className="flex min-w-0 flex-col gap-0.5 text-[0.78125rem] text-ink">{children}</span>
    </label>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
    return <label className="flex flex-col gap-1"><span className={formLabel}>{label}</span>{children}</label>;
}

/**
 * Create a capability: four steps, in place of the manage view's list.
 * 1 asks what the channel should learn; 2 asks the details of that path,
 * checking live (lib/capabilityFit) whether the channel already holds
 * something that does it; 3 asks for a name and a description; 4 confirms
 * what the work stack will make. A fit's own action (show a handler, make a
 * renderer the default, make a runner) ends the questionnaire with no
 * spawn; only Open work stack spawns, with every answer shown.
 */
export default function CreateQuestionnaire({ catalogs, prefs, initialDescription = '', onCancel, onExit }: {
    catalogs: FitCatalogs;
    /** The channel's renderer preferences ('Make it the default' extends them). */
    prefs: Record<string, string>;
    /** 'What should it do?' prefilled (a search that found nothing). */
    initialDescription?: string;
    onCancel: () => void;
    onExit: (exit: CreateExit) => void;
}) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const [step, setStep] = useState(1);
    const [answers, setAnswers] = useState<FitAnswers>({ path: null });
    const [name, setName] = useState('');
    const [what, setWhat] = useState(initialDescription);
    const [follow, setFollow] = useState('');
    const [make, setMake] = useState<CapabilityKind[]>([]);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const assessment = assessFit(answers, catalogs);
    const types = knownTypes(catalogs);
    const path = FIT_PATHS.find(item => item.path === answers.path);
    const set = (patch: Partial<FitAnswers>) => setAnswers(prev => ({ ...prev, ...patch }));

    const act = async (fn: () => Promise<void>) => {
        setBusy(true); setError('');
        try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : 'Something went wrong'); } finally { setBusy(false); }
    };
    const description = what.trim() || initialDescription;

    const fitActions = (fit: Fit): ReactNode => {
        switch (fit.kind) {
            case 'handled': return <>
                <button type="button" className={btn} onClick={() => onExit({ kind: 'handler', cellId: fit.handler.cellId, folder: fit.handler.folder })}>Show it</button>
                <button type="button" className={btn} onClick={() => onExit({ kind: 'handler', cellId: fit.handler.cellId, folder: fit.handler.folder, step: 'addExample' })}>Add an example</button>
            </>;
            case 'shown': return <>
                <button type="button" className={btn} disabled={busy || prefs[fit.ext] === fit.renderer.id}
                    onClick={() => void act(async () => {
                        await makeDefaultRenderer(prefs, fit.ext, fit.renderer.id);
                        onExit({ kind: 'notice', text: `‘${fit.renderer.name}’ is now the default for ${fit.ext} files.` });
                    })}>Make it the default</button>
                {!answers.newView && <button type="button" className={btn} onClick={() => set({ newView: true })}>I still need a new view</button>}
            </>;
            case 'nothing-new': return <button type="button" className={btn} onClick={onCancel}>Done</button>;
            case 'change-handler': return <button type="button" className={btn} onClick={() => onExit({ kind: 'handler', cellId: fit.handler.cellId, folder: fit.handler.folder })}>Show it</button>;
            case 'runner-enough': return <button type="button" className={btn} onClick={() => onExit({ kind: 'runner', generatorId: fit.generator.id, description })}>Make a runner</button>;
            case 'runner-of': return <div className="flex w-full flex-col gap-1.5">
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                    {fit.generators.map(generator => <li key={generator.id} className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 text-[0.78125rem] text-ink">{generator.name}{generator.summary && <span className="text-ink-2"> · {generator.summary}</span>}</span>
                        <button type="button" className={btn} onClick={() => onExit({ kind: 'runner', generatorId: generator.id, description })}>Make a runner for it</button>
                    </li>)}
                </ul>
                {!answers.noneFit && <button type="button" className={cn(btn, 'self-start')} onClick={() => set({ noneFit: true })}>None fit</button>}
            </div>;
            case 'reuse': return null;
        }
    };
    const fits = assessment.fits.length > 0 && <div data-capability-fits="" className="flex flex-col gap-2">
        {assessment.fits.map((fit, at) => <div key={at} role="status" data-fit={fit.kind} className={cn(card, 'flex flex-col gap-1.5 px-2.5 py-2')}>
            <p className="text-[0.78125rem] leading-[1.45] text-ink">{fitText(fit)}</p>
            <div className="flex flex-wrap gap-1.5">{fitActions(fit)}</div>
        </div>)}
    </div>;

    const extInput = (placeholder: string) => <Field label={FIT_QUESTIONS.ext}>
        <input className={formInput} aria-label={FIT_QUESTIONS.ext} placeholder={placeholder} value={answers.ext ?? ''}
            onChange={e => set({ ext: e.target.value, newView: false })} onBlur={() => set({ ext: normalizeExt(answers.ext) })} />
    </Field>;
    const typeSelect = (label: string, value: string | undefined, onChange: (value: string) => void) => <Field label={label}>
        <select className={formSelect} aria-label={label} value={value ?? ''} onChange={e => onChange(e.target.value)}>
            <option value="">Choose a type…</option>
            {types.map(type => <option key={type} value={type}>{type}</option>)}
        </select>
    </Field>;

    const details = (): ReactNode => {
        switch (answers.path) {
            case 'file-type': return <>
                {extInput('.recipe')}
                {fits}
                {!assessment.fits.some(fit => fit.ends) && normalizeExt(answers.ext) && <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
                    <legend className={cn(formLabel, 'mb-1')}>{FIT_QUESTIONS.look}</legend>
                    {(Object.keys(LOOK_LABELS) as (keyof typeof LOOK_LABELS)[]).map(look =>
                        <Radio key={look} name="look" checked={answers.look === look} onChange={() => set({ look })}>{LOOK_LABELS[look]}</Radio>)}
                    {answers.look === 'existing' && typeSelect(FIT_QUESTIONS.lookAs, answers.lookAs, lookAs => set({ lookAs }))}
                </fieldset>}
            </>;
            case 'show': return <>
                {extInput('.csv')}
                {fits}
                {normalizeExt(answers.ext) && <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[0.78125rem] text-ink-2">{FIT_QUESTIONS.variant}</span>
                    <button type="button" className={btn} onClick={() => setAnswers({ path: 'variant', ownExt: normalizeExt(answers.ext) })}>Yes</button>
                </div>}
            </>;
            case 'variant': return <>
                {typeSelect(FIT_QUESTIONS.base, answers.base, base => set({ base }))}
                <Field label={`${FIT_QUESTIONS.ownExt} (optional)`}>
                    <input className={formInput} aria-label={FIT_QUESTIONS.ownExt} placeholder=".notes" value={answers.ownExt ?? ''}
                        onChange={e => set({ ownExt: e.target.value })} onBlur={() => set({ ownExt: normalizeExt(answers.ownExt) })} />
                </Field>
                <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
                    <legend className={cn(formLabel, 'mb-1')}>{FIT_QUESTIONS.checks}</legend>
                    <div className="flex gap-1.5">
                        <Radio name="checks" checked={answers.checks === true} onChange={() => set({ checks: true })}>Yes</Radio>
                        <Radio name="checks" checked={answers.checks === false} onChange={() => set({ checks: false })}>No</Radio>
                    </div>
                </fieldset>
                {fits}
            </>;
            case 'program': return <>
                <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
                    <legend className={cn(formLabel, 'mb-1')}>{FIT_QUESTIONS.generator}</legend>
                    {catalogs.generators.map(generator => <Radio key={generator.id} name="generator" checked={answers.generator === generator.id} onChange={() => set({ generator: generator.id })}>
                        <span className="font-medium">{generator.name}</span>
                        {generator.summary && <span className="text-ink-2">{generator.summary}</span>}
                    </Radio>)}
                    <Radio name="generator" checked={answers.generator === null} onChange={() => set({ generator: null })}>None of these</Radio>
                </fieldset>
                {fits}
            </>;
            case 'making': return <>
                <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
                    <legend className={cn(formLabel, 'mb-1')}>{FIT_QUESTIONS.how}</legend>
                    {(Object.keys(HOW_LABELS) as (keyof typeof HOW_LABELS)[]).map(how =>
                        <Radio key={how} name="how" checked={answers.how === how} onChange={() => set({ how, noneFit: false })}>{HOW_LABELS[how]}</Radio>)}
                </fieldset>
                {fits}
            </>;
            case 'unsure':
            case null:
                return <p className="text-[0.78125rem] text-ink-2">Let the work stack decide: describe it next, and the brief leaves the choice of what to make to it.</p>;
        }
    };

    const answerList = () => [
        ...stepAnswers(answers, catalogs),
        { question: FIT_QUESTIONS.name, answer: name.trim() },
        { question: FIT_QUESTIONS.what, answer: what.trim() },
        ...(follow.trim() ? [{ question: FIT_QUESTIONS.follow, answer: follow.trim() }] : []),
    ];
    const confirm = () => act(async () => {
        const result = await spawnCapabilityCreate(channel, {
            title: name.trim(), make, answers: answerList(),
            related: assessment.related.map(item => ({ kind: item.kind, id: item.id })),
        });
        openSpawnedCell(result);
        onExit({ kind: 'notice', text: `Work stack ‘Create ${name.trim()}’ is open. Run any runner there. A runner that starts from the open file follows brief.md; with any other, tell it to follow brief.md. Then add what it makes from its Files panel.` });
    });

    const nextEnabled = step === 1 ? !!answers.path
        : step === 2 ? readyToDescribe(answers, assessment)
            : step === 3 ? !!name.trim() && !!what.trim() : false;
    const next = () => {
        if (step === 3) setMake(assessment.make);
        setStep(step + 1);
    };

    return <div data-create-questionnaire="" className="flex flex-col gap-3">
        <ol aria-label="Steps" className="m-0 flex list-none flex-wrap gap-x-3 gap-y-1 p-0">
            {STEP_NAMES.map((label, at) => <li key={label} aria-current={step === at + 1 ? 'step' : undefined}
                className={cn(sectionLabel, step === at + 1 ? 'text-teal' : step > at + 1 ? 'text-ink-2' : 'text-ink-3')}>{at + 1}. {label}</li>)}
        </ol>
        {step === 1 && <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
            <legend className="mb-1.5 text-[0.875rem] font-semibold text-ink">{FIT_QUESTIONS.path}</legend>
            {FIT_PATHS.map(item => <Radio key={item.path} name="path" checked={answers.path === item.path} onChange={() => setAnswers({ path: item.path as FitPath })}>
                <span className="font-medium">{item.label}</span>
                {item.path !== 'unsure' && <span className="text-ink-3">e.g. ‘{item.example}’</span>}
            </Radio>)}
        </fieldset>}
        {step === 2 && <section aria-label={path?.label} className="flex flex-col gap-2.5">
            <h5 className="text-[0.875rem] font-semibold text-ink">{path?.label}</h5>
            {details()}
        </section>}
        {step === 3 && <section aria-label="Describe it" className="flex flex-col gap-2.5">
            <h5 className="text-[0.875rem] font-semibold text-ink">Describe it</h5>
            <Field label={FIT_QUESTIONS.name}>
                <input className={formInput} aria-label={FIT_QUESTIONS.name} placeholder="Recipe" maxLength={80} value={name} onChange={e => setName(e.target.value)} />
            </Field>
            <Field label={FIT_QUESTIONS.what}>
                <textarea className={formTextarea} aria-label={FIT_QUESTIONS.what} required rows={4} placeholder={path?.example} value={what} onChange={e => setWhat(e.target.value)} />
            </Field>
            <Field label={FIT_QUESTIONS.follow}>
                <textarea className={formTextarea} aria-label={FIT_QUESTIONS.follow} rows={3} placeholder="a recipe has a title, ingredients and steps" value={follow} onChange={e => setFollow(e.target.value)} />
            </Field>
        </section>}
        {step === 4 && <section aria-label="Confirm" className="flex flex-col gap-2.5">
            <fieldset className="m-0 flex flex-col gap-1 border-0 p-0">
                <legend className={cn(formLabel, 'mb-1')}>You will make:</legend>
                {CAPABILITY_KINDS.map(kind => <label key={kind} className="flex items-center gap-2 text-[0.78125rem] text-ink">
                    <input type="checkbox" className="accent-teal" checked={make.includes(kind)}
                        onChange={e => setMake(e.target.checked ? CAPABILITY_KINDS.filter(item => item === kind || make.includes(item)) : make.filter(item => item !== kind))} />
                    {KIND_LINES[kind]}
                </label>)}
                {make.length === 0 && <p className="text-[0.75rem] text-ink-3">Nothing chosen: the work stack decides what to make.</p>}
            </fieldset>
            {assessment.related.length > 0 && <p className="text-[0.78125rem] text-ink-2">Related in this channel: {assessment.related.map(item => item.name).join(', ')}</p>}
            <p className="text-[0.75rem] leading-[1.45] text-ink-3">A new stack opens beside Capabilities with a brief of your answers and the authoring guides. Run any runner there. A runner that starts from the open file follows brief.md; with any other, tell it to follow brief.md. When its files are ready, add each one from that stack’s Files panel — a generator before its runners. A new handler then needs a Build from its card.</p>
        </section>}
        {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2 border-t border-line pt-3">
            {step === 4
                ? <>
                    <button type="button" className={btn} disabled={busy} onClick={() => setStep(3)}>Back</button>
                    <button type="button" className={btnPrimary} disabled={busy} onClick={() => void confirm()}>Open work stack</button>
                </>
                : <>
                    <button type="button" className={btn} onClick={onCancel}>Cancel</button>
                    {step > 1 && <button type="button" className={btn} onClick={() => setStep(step - 1)}>Back</button>}
                    <button type="button" className={btnPrimary} disabled={!nextEnabled} onClick={next}>Next</button>
                </>}
        </div>
    </div>;
}
