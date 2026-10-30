import { Plus } from 'lucide-react';
import type { GeneratorEntry } from '@/lib/api';
import type { RunnerGenerator } from '@/lib/runnerApi';
import { btn, chip } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** What a runner author needs to know of one generator, as short mono
 *  facts: how it runs, its version and entrypoint, its settings file and
 *  field names, the UI files it declares, the config folders it accepts,
 *  its mid-run file editing and its configs. */
function generatorFacts(generator: GeneratorEntry, schema?: RunnerGenerator): string[] {
    const facts = [generator.daemon ? 'daemon' : 'one-shot'];
    if (generator.chat) facts.push('chat');
    if (generator.requires_docker) facts.push('docker');
    if (generator.version) facts.push(`v${generator.version}`);
    if (generator.entrypoint) facts.push(generator.entrypoint);
    if (schema) facts.push(`${schema.main_config}: ${schema.fields.map(field => field.key).join(', ') || 'no fields'}`);
    if (generator.ui_files.length) facts.push(`ui ${generator.ui_files.join(' ')}`);
    if (generator.config_folders.length) facts.push(`accepts ${generator.config_folders.map(folder => folder.name).join(', ')}`);
    if (generator.live_files_mode !== 'none') facts.push(`live files: ${generator.live_files_mode}`);
    if (schema && schema.configs.length > 1) facts.push(`configs ${schema.configs.map(config => config.name).join(', ')}`);
    return facts;
}

/** The Capabilities manage view's generator rows: one row each between
 *  line rules, its name over its id in mono, its summary, a line of facts
 *  and the cell that holds it. A click on the row (or its cell chip)
 *  brings that cell to the front of the stack; 'Add runner' starts a runner
 *  of this generator. Nothing here edits a generator; its cell's files do. */
export function GeneratorList({ generators, schemas, onShowCell, onAddRunner }: {
    generators: readonly GeneratorEntry[];
    /** Each generator's runner schema, by generator id. */
    schemas: Readonly<Record<string, RunnerGenerator>>;
    onShowCell: (cellId: string) => void;
    onAddRunner: (generator: GeneratorEntry) => void;
}) {
    return <div data-generator-list="" className="flex flex-col">
        {generators.map(generator => (
            <div key={generator.name} data-generator-row={generator.name} role="button" tabIndex={0}
                aria-label={`${generator.display_name}: open its cell`}
                className="flex cursor-pointer flex-col border-t border-line px-2 py-3 last:border-b hover:bg-sheet-2"
                onClick={() => onShowCell(generator.cell_id)}
                onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onShowCell(generator.cell_id); } }}>
                <div className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 flex-1 truncate font-semibold text-ink">{generator.display_name}</span>
                    <button type="button" className={btn} onClick={e => { e.stopPropagation(); onAddRunner(generator); }}>
                        <Plus className="h-3.5 w-3.5" aria-hidden />Add runner
                    </button>
                </div>
                <span className="mt-px truncate font-mono text-[0.75rem] text-ink-2">{generator.name}</span>
                {generator.summary && <p className="mt-0.5 text-[0.78125rem] leading-[1.45] text-ink-2">{generator.summary}</p>}
                <div data-generator-facts="" className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[0.6875rem] text-ink-3">
                    {generatorFacts(generator, schemas[generator.name]).map(fact => <span key={fact}>{fact}</span>)}
                    <button type="button" className={cn(chip, 'font-sans hover:text-ink')} title="Bring its cell to the front"
                        onClick={e => { e.stopPropagation(); onShowCell(generator.cell_id); }}>in {generator.group || 'its cell'}</button>
                </div>
            </div>
        ))}
    </div>;
}
