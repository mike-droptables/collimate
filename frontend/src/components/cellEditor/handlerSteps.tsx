import { useState, type ReactNode } from 'react';
import { BadgeCheck, Layers, Plus, RotateCcw, X } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { BLANK_EXAMPLE, type BuildInspection, type ExampleDeclaration, type HandlerInfo } from '@/lib/definitionApi';
import { compareExamples, exampleSources, matchCount, type ExampleVerdict } from '@/lib/handlerExamples';
import { clashLine, handlerState, hint, short } from '@/lib/handlerStatus';
import { ExampleFields } from './ExampleFields';
import { btn, btnPrimary, formLabel, formSelect, iconBtn, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

const MONO = 'font-mono text-[0.75rem] text-ink-2 break-all';
const DOT = 'size-1.5 flex-none rounded-full';

/** An inline step under a handler card's actions: its title and a Close. */
export function StepFrame({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
    return <section aria-label={title} data-handler-step="" className="flex flex-col gap-2 rounded-md border border-line bg-sheet-2 px-2.5 py-2">
        <div className="flex items-center gap-2">
            <h5 className={cn(sectionLabel, 'min-w-0 flex-1')}>{title}</h5>
            <button type="button" aria-label="Close" title="Close" className={cn(iconBtn, 'size-5')} onClick={onClose}><X className="size-3.5" aria-hidden /></button>
        </div>
        {children}
        {footer && <div className="flex flex-wrap items-center justify-end gap-2">{footer}</div>}
    </section>;
}

/** A label and its value on one line (the review summary). */
export function SummaryRow({ term, children }: { term: string; children: ReactNode }) {
    return <div className="flex min-h-[1.625rem] items-baseline gap-2.5 py-[0.1875rem]">
        <dt className="min-w-[6rem] flex-none text-[0.75rem] text-ink-2">{term}</dt>
        <dd className="m-0 min-w-0 flex-1 break-words text-[0.78125rem] text-ink">{children}</dd>
    </div>;
}

/** A build's 8-character name, its full hash on hover. */
export const Hash = ({ value }: { value: string | null | undefined }) => value
    ? <code title={value} className="font-mono text-[0.75rem] text-ink-2">{short(value)}</code>
    : <span className="text-ink-3">None</span>;

/** Each example's verdict lines (the review's expanded Examples row). */
export function VerdictList({ verdicts }: { verdicts: ReadonlyMap<string, ExampleVerdict[]> }) {
    return <ul aria-label="Example results" className="m-0 flex list-none flex-col gap-0.5 p-0">
        {[...verdicts].flatMap(([folder, files]) => files.map((verdict, at) =>
            <li key={`${folder}:${at}`} data-outcome={verdict.pass ? 'pass' : 'fail'} className="flex items-start gap-2 py-[0.1875rem]">
                <span role="img" aria-label={verdict.pass ? 'Matches' : 'Mismatch'} className={cn(DOT, 'mt-[0.375rem]', verdict.pass ? 'bg-green' : 'bg-red')} />
                <span className="flex min-w-0 flex-col">
                    <span className="break-all font-mono text-[0.75rem] text-ink">{verdict.path ? `${folder}/${verdict.path}` : folder}</span>
                    <span className={cn('text-[0.75rem] leading-[1.4]', verdict.pass ? 'text-ink-2' : 'text-red')}>{verdict.message}</span>
                </span>
            </li>))}
    </ul>;
}

/** The Review and accept step: what the waiting build holds and how its
 *  examples fare, then Accept (exactly the inspected content), and on a
 *  built-in the way back to the app's skills. */
export function ReviewStep({ handler, inspection, busy, onAccept, onRevert, onClose }: {
    handler: HandlerInfo;
    inspection: BuildInspection;
    busy: boolean;
    onAccept: () => void;
    /** A built-in whose channel has regenerated skills: Use the app's skills. */
    onRevert?: () => void;
    onClose: () => void;
}) {
    const [expanded, setExpanded] = useState(false);
    const verdicts = compareExamples(inspection);
    const { matched, total } = matchCount(verdicts);
    const compiles = handler.builtin || inspection.compile?.valid !== false;
    const name = short(inspection.derived_hash);
    const clash = clashLine(handler);
    const skills = [
        handler.accepted_skills.length ? handler.accepted_skills.join(', ') : '',
        handler.added_skills.length ? `added ${handler.added_skills.join(', ')}` : '',
    ].filter(Boolean).join(' · ');
    const acceptLabel = handler.builtin ? `Accept skills ${name}` : `Accept build ${name}`;
    return <StepFrame title={handler.builtin ? `Review skills ${name}` : `Review build ${name}`} onClose={onClose}
        footer={<>
            {onRevert && <button type="button" className={btn} disabled={busy} title={hint('revert', handler)} aria-description={hint('revert', handler)}
                onClick={onRevert}><RotateCcw className="h-3.5 w-3.5" aria-hidden />Use the app’s skills</button>}
            <button type="button" className={btnPrimary} disabled={busy || !compiles}
                title={compiles ? hint('review', handler) : 'Its checks must compile before it can be accepted'}
                onClick={onAccept}><BadgeCheck className="h-3.5 w-3.5" aria-hidden />{acceptLabel}</button>
        </>}>
        <dl className="m-0 flex flex-col">
            <SummaryRow term="Status">{handlerState(handler).label}</SummaryRow>
            <SummaryRow term="Checks">
                {handler.builtin ? 'The app’s, unchanged'
                    : compiles ? 'Compile' : <span className="text-red">{inspection.compile.error || 'Do not compile'}</span>}
            </SummaryRow>
            <SummaryRow term="Examples">
                {total
                    ? <button type="button" aria-expanded={expanded} className="text-left text-ink underline decoration-line-2 underline-offset-2 hover:decoration-ink-2"
                        onClick={() => setExpanded(open => !open)}>{matched} of {total} match</button>
                    : <span className="text-ink-3">None declared</span>}
            </SummaryRow>
            {expanded && <VerdictList verdicts={verdicts} />}
            <SummaryRow term={handler.builtin ? 'Skills' : 'Build'}><Hash value={inspection.derived_hash} /></SummaryRow>
            <SummaryRow term="Skills in it">{skills || <span className="text-ink-3">None</span>}</SummaryRow>
            {clash && <SummaryRow term="Clashes"><span className="text-red">{clash}</span></SummaryRow>}
        </dl>
    </StepFrame>;
}

/** The Add example step: the cell the files come from (any cell but a
 *  built-in card holding a file of the handler's types), the files, and
 *  what the example declares. */
export function AddExampleForm({ handler, busy, onAdd, onClose }: {
    handler: HandlerInfo;
    busy: boolean;
    onAdd: (sourceCellId: string, paths: string[], declaration: ExampleDeclaration) => void;
    onClose: () => void;
}) {
    const activeChannel = useCollimateStore(s => s.activeChannel);
    const cellCache = useCollimateStore(s => s.cellCache);
    const [sourceCellId, setSourceCellId] = useState('');
    const [paths, setPaths] = useState<string[]>([]);
    const [declaration, setDeclaration] = useState<ExampleDeclaration>(BLANK_EXAMPLE);
    // The layout's cells, with the cache's fresher files.
    const cells = [...(activeChannel?.main_stacks ?? []), ...(activeChannel?.nav_stacks ?? [])]
        .flatMap(stack => stack.cells).map(cell => (cellCache[cell.id] ? { ...cell, ...cellCache[cell.id] } : cell));
    const sources = exampleSources(cells, handler);
    const source = sources.find(item => item.cell.id === sourceCellId);
    return <StepFrame title={`Add an example to ‘${handler.name}’`} onClose={onClose}
        footer={<button type="button" className={btnPrimary} disabled={busy || !source || !paths.length}
            onClick={() => onAdd(sourceCellId, paths, declaration)}><Plus className="h-3.5 w-3.5" aria-hidden />Add example</button>}>
        <label className="flex flex-col gap-1">
            <span className={formLabel}>From cell</span>
            <select className={formSelect} aria-label="From cell" value={sourceCellId} disabled={busy}
                onChange={e => { setSourceCellId(e.target.value); setPaths([]); }}>
                <option value="">{sources.length ? 'Choose a cell' : `No cell has a ${handler.types.map(type => type.ext).join(' or ')} file`}</option>
                {sources.map(item => <option key={item.cell.id} value={item.cell.id}>{item.cell.name || 'Untitled cell'}</option>)}
            </select>
        </label>
        {source && <fieldset className="m-0 flex flex-col gap-1 border-0 p-0">
            <legend className={formLabel}>Files</legend>
            {source.paths.map(path => <label key={path} className="flex items-center gap-2 text-[0.78125rem] text-ink-2">
                <input type="checkbox" className="accent-teal" checked={paths.includes(path)} disabled={busy}
                    onChange={e => setPaths(e.target.checked ? [...paths, path] : paths.filter(item => item !== path))} />
                <span className={MONO}>{path}</span>
            </label>)}
        </fieldset>}
        <ExampleFields value={declaration} onChange={setDeclaration} disabled={busy} />
    </StepFrame>;
}

/** The Build… step of a user handler, or the Regenerate skills… step of a
 *  built-in: what the work stack beside Capabilities will hold and how its
 *  result comes back, then Open work stack. Nothing runs here: the user
 *  runs any runner in that stack. */
export function WorkStackStep({ handler, busy, onOpen, onClose }: {
    handler: HandlerInfo;
    busy: boolean;
    onOpen: () => void;
    onClose: () => void;
}) {
    const examples = `${handler.examples.length} example${handler.examples.length === 1 ? '' : 's'}`;
    const title = handler.builtin ? `Regenerate ‘${handler.name}’ skills in a work stack` : `Build ‘${handler.name}’ in a work stack`;
    return <StepFrame title={title} onClose={onClose}
        footer={<>
            <button type="button" className={btn} disabled={busy} onClick={onClose}>Cancel</button>
            <button type="button" className={btnPrimary} disabled={busy} title={hint(handler.builtin ? 'regenerate' : 'build', handler)}
                onClick={onOpen}><Layers className="h-3.5 w-3.5" aria-hidden />Open work stack</button>
        </>}>
        <p className="text-[0.78125rem] leading-[1.45] text-ink">
            {handler.builtin
                ? `Opens a new stack with this built-in handler, this channel’s added examples and its current skills, and a brief for rewriting the skills. Its checks never change. When it’s done, use ‘Use these skills’ on ${handler.id}/${handler.id}.handler in that stack’s Files panel; the skills then wait here for review.`
                : `Opens a new stack beside Capabilities with a copy of this handler and a brief for writing its checks (derived/verify.ts) and agent skills (derived/skills/). Run any runner there. When it’s done, add ${handler.folder}/ back from that stack’s Files panel. The new build then waits here for your review; the build in use stays in use.`}
        </p>
        {!handler.builtin && <ul aria-label="What the work stack holds" className="m-0 flex list-none flex-col gap-0.5 p-0">
            <li className={MONO}>Copies {handler.folder}/ ({examples}, current build {short(handler.built_hash) || 'none'}) · brief.md · guides/</li>
            {handler.added_skills.length > 0 && <li className={MONO}>Stays here: added skills (skills/)</li>}
        </ul>}
        <p className="text-[0.75rem] leading-[1.45] text-ink-3">Runners that start from the open file follow brief.md; with others, tell it to follow brief.md.</p>
    </StepFrame>;
}
