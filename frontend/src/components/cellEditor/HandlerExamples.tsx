import { useState } from 'react';
import { ChevronRight, Trash2 } from 'lucide-react';
import type { ExampleDeclaration, HandlerExample, HandlerInfo } from '@/lib/definitionApi';
import { examplePrefix, matchCount, type ExampleVerdict } from '@/lib/handlerExamples';
import { hint } from '@/lib/handlerStatus';
import { ExampleFields } from './ExampleFields';
import { btn, btnDanger, btnPrimary, chip } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

const DOT = 'size-1.5 flex-none rounded-full';

/** The disclosure's count line: how many examples, by what they expect. */
function counts(examples: readonly HandlerExample[]): string {
    const valid = examples.filter(example => example.expect === 'valid').length;
    const invalid = examples.length - valid;
    return [valid ? `${valid} expect valid` : '', invalid ? `${invalid} invalid` : ''].filter(Boolean).join(', ');
}

/** One example: its folder, what it shows and expects, its files, and after
 *  a Validate or a Review its verdict. An editable one edits its
 *  description, verdict and feedback in place, or goes. */
function ExampleCard({ handler, example, files, verdicts, editable, busy, onOpen, onSave, onRemove }: {
    handler: HandlerInfo;
    example: HandlerExample;
    files: string[];
    verdicts: ExampleVerdict[] | undefined;
    editable: boolean;
    busy: boolean;
    onOpen: (path: string) => void;
    onSave: (fields: ExampleDeclaration) => void;
    onRemove: () => void;
}) {
    const [draft, setDraft] = useState<ExampleDeclaration>({ description: example.description, expect: example.expect, feedback: example.feedback });
    const changed = draft.description !== example.description || draft.expect !== example.expect || draft.feedback !== example.feedback;
    const pass = verdicts && verdicts.length > 0 && verdicts.every(verdict => verdict.pass);
    return <li data-example={example.folder} data-verdict={verdicts ? (pass ? 'match' : 'mismatch') : undefined}
        className="flex flex-col gap-2 rounded-md border border-line px-2.5 py-2">
        <div className="flex items-center gap-2">
            {verdicts && <span role="img" aria-label={pass ? 'Matches' : 'Mismatch'} className={cn(DOT, pass ? 'bg-green' : 'bg-red')} />}
            <span className="min-w-0 flex-1 break-all font-mono text-[0.75rem] text-ink">{example.folder}</span>
            {handler.builtin && <span className={chip}>{example.added ? 'Added' : 'Built-in'}</span>}
        </div>
        {editable
            ? <ExampleFields value={draft} onChange={setDraft} disabled={busy} />
            : <dl className="m-0 flex flex-col gap-1 text-[0.78125rem]">
                {example.description && <dd className="m-0 text-ink-2">{example.description}</dd>}
                <div className="flex gap-2"><dt className="text-ink-3">Expects</dt><dd className="m-0 text-ink-2">{example.expect}</dd></div>
                {example.feedback && <div className="flex gap-2"><dt className="text-ink-3">Feedback</dt><dd className="m-0 text-ink-2">{example.feedback}</dd></div>}
            </dl>}
        {verdicts && <ul aria-label={`Results of ${example.folder}`} className="m-0 flex list-none flex-col gap-0.5 p-0">
            {verdicts.map((verdict, at) => <li key={at} className={cn('text-[0.75rem] leading-[1.4]', verdict.pass ? 'text-ink-2' : 'text-red')}>
                {verdict.path && <span className="font-mono">{verdict.path}: </span>}{verdict.message}
            </li>)}
        </ul>}
        <div className="flex flex-wrap gap-1.5">
            {files.map(path => <button key={path} type="button" className={cn(btn, 'font-mono text-[0.75rem]')} title={`Open ${path}`}
                onClick={() => onOpen(path)}>{path.slice(path.lastIndexOf('/') + 1)}</button>)}
            {editable && <>
                <span className="flex-1" />
                <button type="button" className={btnDanger} disabled={busy} title={hint('removeExample', handler)} aria-description={hint('removeExample', handler)}
                    onClick={onRemove}><Trash2 className="h-3.5 w-3.5" aria-hidden />Remove</button>
                <button type="button" className={btnPrimary} disabled={busy || !changed} onClick={() => onSave(draft)}>Save</button>
            </>}
        </div>
    </li>;
}

/** A handler card's examples: a disclosure (`Examples · N`, by what they
 *  expect, and after a Validate how many match) over one card per example.
 *  The app's examples of a built-in are read-only; every other example
 *  edits in place. */
export function HandlerExamples({ handler, cellFiles, verdicts, open, onToggle, busy, onOpen, onSave, onRemove }: {
    handler: HandlerInfo;
    /** The names of the files of the handler's cell. */
    cellFiles: readonly string[];
    verdicts: ReadonlyMap<string, ExampleVerdict[]> | null;
    open: boolean;
    onToggle: () => void;
    busy: boolean;
    onOpen: (path: string) => void;
    onSave: (folder: string, fields: ExampleDeclaration) => void;
    onRemove: (folder: string) => void;
}) {
    const { examples } = handler;
    const tally = verdicts ? matchCount(verdicts) : null;
    return <div data-handler-examples="" className="flex flex-col gap-1.5">
        <button type="button" aria-expanded={open} disabled={!examples.length}
            className="flex items-center gap-1.5 text-left text-[0.78125rem] text-ink-2 hover:text-ink disabled:cursor-default disabled:hover:text-ink-2"
            onClick={onToggle}>
            <ChevronRight className={cn('size-3.5 flex-none transition-transform', open && 'rotate-90', !examples.length && 'opacity-0')} aria-hidden />
            <span className="font-medium text-ink">Examples · {examples.length}</span>
            {examples.length > 0 && <span className="text-ink-3">({counts(examples)})</span>}
            {tally && tally.total > 0 && <span className={cn('ml-auto', tally.matched === tally.total ? 'text-green' : 'text-red')}>{tally.matched} of {tally.total} match</span>}
        </button>
        {!examples.length && <p className="pl-5 text-[0.75rem] text-ink-3">No examples yet. Validate and the build learn from examples you add.</p>}
        {open && examples.length > 0 && <ul aria-label="Examples" className="m-0 flex list-none flex-col gap-2 p-0">
            {/* The key remounts a card when its saved declaration changes,
                so its draft starts from what the cell holds. */}
            {examples.map(example => <ExampleCard key={`${example.folder}:${example.description}:${example.expect}:${example.feedback}`}
                handler={handler} example={example} busy={busy}
                editable={!handler.builtin || !!example.added}
                files={cellFiles.filter(path => path.startsWith(examplePrefix(handler, example.folder)))}
                verdicts={verdicts?.get(example.folder)}
                onOpen={onOpen}
                onSave={fields => onSave(example.folder, fields)}
                onRemove={() => onRemove(example.folder)} />)}
        </ul>}
    </div>;
}
