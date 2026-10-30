import {hydrateFiles,binaryFile} from '@/lib/fileCommands';
import {flushFileDocuments} from '@/lib/fileDocuments';
import { useEffect, useMemo, useState } from 'react';
import { DefinitionForm } from './DefinitionForm';
import { definitionKind, definitionText, folderOf, parseDefinition, previewPath, replaceFiles, textFile } from '@/lib/definitionFiles';
import { isCapabilitiesStack, type VirtualFile } from '@/lib/types';
import type { UpdateFiles } from '@/lib/fileTransfer';
import { fetchTextBodies } from '@/lib/fileTransfer';
import { fetchRunnerGenerators, fetchRunnerConfig, fetchRunners, type RunnerDraft, type RunnerGenerator, type RunnerEntry, type RunnerTemplate } from '@/lib/runnerApi';
import { RunnerEditor } from '@/components/runner/RunnerEditor';
import { uploadBlob } from '@/lib/api';
import { effectiveValues, type ParsedSetYaml } from '@/lib/setYaml';
import { fetchHandlers, fetchOptionsMap, type OptionsMap } from '@/lib/definitionApi';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { revealHandler } from '@/lib/revealFile';
import { useRendererPreviewHash } from '@/lib/rendererPreviewEvidence';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { btn, formSelect, sectionLabel } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';

/** A kind-specific block under the definition's fields. */
const SECTION = 'flex flex-col gap-2 border-t border-line pt-3';
const NOTE = 'text-[0.78125rem] text-ink-2';
const ALERT = 'text-[0.75rem] text-red';
const HASH = 'font-mono text-[0.75rem] text-ink break-all';
const LIST = 'm-0 flex list-none flex-col gap-0.5 p-0 font-mono text-[0.75rem] text-ink-2';

interface Props { versionId?: string; path: string; content: string; files: VirtualFile[]; cellId: string; channel: string; readOnly: boolean; onSave: (text: string) => void | Promise<void>; onUpdateFiles?: UpdateFiles; onOpenFile?: (path: string) => void; }

export default function DefinitionViewer(props: Props) {
    const kind = definitionKind(props.path);
    if (kind === 'runner') return <RunnerDefinitionViewer {...props} />;
    return <GenericDefinitionViewer {...props} />;
}

function GenericDefinitionViewer({ path, content, files, cellId, channel, versionId, readOnly, onSave, onUpdateFiles, onOpenFile }: Props) {
    const [sample, setSample] = useState('');
    const [error, setError] = useState('');
    const [runners, setRunners] = useState<RunnerEntry[]>([]);
    const mountedHash = useRendererPreviewHash(cellId, path);
    const [mapping, setMapping] = useState<OptionsMap | null>(null);
    const [mappingError, setMappingError] = useState('');
    const [mappingRevision, setMappingRevision] = useState(0);
    const kind = definitionKind(path);
    // A built-in card's handler: the app's validation, the channel's added
    // examples beside the app's own.
    const builtin = useCollimateStore(state => !!state.locateCell(cellId)?.cell.builtin);
    // Only a definition on Capabilities is in use: its runners, its card.
    const inCapabilitiesStack = useCollimateStore(state => [...(state.activeChannel?.nav_stacks ?? []), ...(state.activeChannel?.main_stacks ?? [])].some(stack => isCapabilitiesStack(stack) && stack.cells.some(cell => cell.id === cellId)));
    const manifest = useMemo(() => { try { return parseDefinition(content); } catch { return {}; } }, [content]);
    // A handler off Capabilities with a built-in handler's id (a Regenerate
    // work cell's copy) goes back through Use these skills, never Add: the
    // built-in handlers' ids, null while they load (or failed: no claim).
    const handlerOffCapabilities = kind === 'handler' && !builtin && !inCapabilitiesStack;
    const [builtinHandlerIds, setBuiltinHandlerIds] = useState<ReadonlySet<string> | null>(null);
    useEffect(() => {
        if (!handlerOffCapabilities) return;
        let alive = true;
        void fetchHandlers()
            .then(handlers => { if (alive) setBuiltinHandlerIds(new Set(handlers.filter(handler => handler.builtin).map(handler => handler.id))); })
            .catch(() => {});
        return () => { alive = false; };
    }, [handlerOffCapabilities]);
    useEffect(() => {
        if (kind !== 'generator') return;
        let alive = true;
        void fetchRunners().then(value => { if (alive) setRunners(value); }).catch(() => {});
        return () => { alive = false; };
    }, [kind]);
    useEffect(() => {
        if (kind !== 'options') return;
        let alive = true;
        setMapping(null); setMappingError('');
        void fetchOptionsMap(channel, cellId, path, versionId).then(value => { if (alive) setMapping(value); }).catch(e => { if (alive) setMappingError(String(e.message ?? e)); });
        return () => { alive = false; };
    }, [kind, channel, cellId, path, versionId, content, mappingRevision]);
    const refreshMapping = async () => {
        try { if (!readOnly && !versionId) { await flushCellSaves(cellId); await flushFileDocuments(); } setMappingRevision(value => value + 1); }
        catch (e) { setMappingError(e instanceof Error ? e.message : String(e)); }
    };
    const preview = async () => {
        if (!onUpdateFiles || !sample || readOnly) return;
        let created = '';
        await onUpdateFiles(current => {
            created = previewPath(path, current.map(file => file.name));
            return replaceFiles(current, [textFile(created, JSON.stringify({ renderer: path, file: sample }, null, 2))]);
        });
        if (created) onOpenFile?.(created);
    };
    const examples = Array.isArray(manifest.examples) ? manifest.examples : [];
    const configs = Array.isArray(manifest.configs) ? manifest.configs : [];
    return <DefinitionForm key={path} kind={kind} content={content} readOnly={readOnly} onSave={async text => { await onSave(text); if (kind === 'options') await refreshMapping(); }}>
        {error && <p role="alert" className={ALERT}>{error}</p>}
        {kind === 'renderer' && <section className={SECTION}>
            <h3 className={sectionLabel}>Preview renderer</h3>
            <div className="flex items-center gap-2">
                <select aria-label="Preview file" className={formSelect} value={sample} onChange={e => { setSample(e.target.value); setError(''); }}>
                    <option value="">Choose a file</option>
                    {files.filter(file => file.name !== path).map(file => <option key={file.name}>{file.name}</option>)}
                </select>
                <button type="button" className={btn} disabled={readOnly || !onUpdateFiles || !sample} onClick={preview}>Preview</button>
            </div>
            {mountedHash && <p className={NOTE}>Previewed package hash: <code className={HASH}>{mountedHash}</code></p>}
        </section>}
        {kind === 'generator' && <section className={SECTION}>
            <h3 className={sectionLabel}>Named configs</h3>
            <div className="flex flex-wrap gap-1.5">{configs.map((entry: unknown, index: number) => {
                if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return <p key={index} className={ALERT}>Invalid named config {index + 1}; edit configs to repair it.</p>;
                const row = entry as Record<string, unknown>;
                if (typeof row.id !== 'string' || typeof row.path !== 'string' || (row.name !== undefined && typeof row.name !== 'string')) return <p key={index} className={ALERT}>Invalid named config {index + 1}; edit configs to repair it.</p>;
                return <button type="button" key={index} className={btn} disabled={!row.path} onClick={() => onOpenFile?.(`${folderOf(path)}/${row.path}`)}>{String(row.name ?? row.id)}</button>;
            })}</div>
            {inCapabilitiesStack && <>
                <h3 className={cn(sectionLabel, 'mt-1')}>Runners targeting this generator</h3>
                <ul className="m-0 flex list-none flex-col gap-0.5 p-0">{runners.filter(runner => runner.generator === manifest.id).map(runner => <li key={runner.id} className="text-[0.78125rem] text-ink">{runner.name}</li>)}</ul>
            </>}
        </section>}
        {kind === 'options' && <section className={SECTION}>
            <div className="flex items-center justify-between gap-2">
                <h3 className={sectionLabel}>Options and files</h3>
                <button type="button" className={btn} onClick={() => void refreshMapping()}>Refresh mapping</button>
            </div>
            {mappingError && <p role="alert" className={ALERT}>{mappingError}</p>}
            {mapping && <>
                <p className={NOTE}>{mapping.retain_source ? 'Source retained' : 'Source not retained'}</p>
                {mapping.options.map((option, index) => <section key={index} className="flex flex-col gap-1">
                    <h4 className="text-[0.78125rem] font-semibold text-ink">{option.name}</h4>
                    <ul className={LIST}>{Object.entries(option.files).map(([destination, source]) => <li key={destination}>{destination} ← {source}</li>)}</ul>
                </section>)}
                <h4 className="text-[0.78125rem] font-semibold text-ink">Kept files</h4>
                <ul className={LIST}>{mapping.kept.map(name => <li key={name}>{name}</li>)}</ul>
            </>}
        </section>}
        {kind === 'handler' && <section className={SECTION}>
            <h3 className={sectionLabel}>Example files</h3>
            <div className="flex flex-col items-start gap-1">{(builtin
                ? [`${folderOf(path)}/examples/`]
                : examples.flatMap((entry: unknown) => {
                    if (!entry || typeof entry !== 'object' || Array.isArray(entry) || typeof (entry as Record<string, unknown>).folder !== 'string') return [];
                    return [`${folderOf(path)}/examples/${(entry as Record<string, unknown>).folder}/`];
                })).flatMap((prefix, index) => files.filter(file => file.name.startsWith(prefix)).map(file =>
                    <button type="button" key={`${index}:${file.name}`} className={cn(btn, 'font-mono text-[0.75rem]')} onClick={() => onOpenFile?.(file.name)}>{file.name}</button>))}</div>
            {inCapabilitiesStack ? <div className="flex flex-wrap items-center gap-2">
                <p className={NOTE}>{builtin
                    ? 'Built-in: its validation is part of the app. Add examples and regenerate skills from its card in Capabilities.'
                    : 'Validate, build and accept this handler from its card in Capabilities.'}</p>
                <button type="button" className={btn} onClick={() => revealHandler(cellId, folderOf(path))}>Show in Capabilities</button>
            </div> : builtinHandlerIds && <p className={NOTE}>{typeof manifest.id === 'string' && builtinHandlerIds.has(manifest.id)
                ? 'Use these skills from this cell’s Files panel; they then wait for review on the built-in card in Capabilities.'
                : 'Add it to Capabilities from this cell’s Files panel to validate, build and accept it there.'}</p>}
        </section>}
    </DefinitionForm>;
}

function RunnerDefinitionViewer({ path, content, files, cellId, channel, readOnly, onUpdateFiles }: Props) {
    const [reset, setReset] = useState(0);
    const [draft, setDraft] = useState<RunnerDraft | null>(null);
    const [generators, setGenerators] = useState<RunnerGenerator[]>([]);
    const [localConfigs, setLocalConfigs] = useState<Record<string, RunnerTemplate>>({});
    const [error, setError] = useState('');
    const folder = folderOf(path);
    useEffect(() => {
        let alive = true;
        void (async () => {
            const materialized=await hydrateFiles(channel,cellId,files);
            const raw = parseDefinition(content);
            const runner = { description: '', tags: [], prompt: '', settings: {}, accounts: {}, folders: [], permissions: {}, packages: {}, ...raw } as unknown as RunnerDraft['runner'];
            const companions=materialized.filter(file=>file.name.startsWith(folder+'/files/')).map(file=>({name:file.name.slice(folder.length+7),content:file.content,language:file.language,...(file.encoding==='binary'?{blob_hash:file.body_hash!}:{})}));
            const catalog = await fetchRunnerGenerators();
            const configs: Record<string, RunnerTemplate> = {};
            for (const definition of materialized.filter(file => definitionKind(file.name) === 'generator')) {
                const text=definition.content;
                const manifest = parseDefinition(text);
                if (manifest.id !== runner.generator) continue;
                const main = String(manifest.main_config ?? '');
                const schemaFile = materialized.find(file => file.name === `${folderOf(definition.name)}/${main}`);
                if (!schemaFile) throw new Error('The draft generator main config is missing.');
                const schemaText=schemaFile.content;
                const rawSchema = parseDefinition(schemaText);
                const schema = { fields: Array.isArray(rawSchema.fields) ? rawSchema.fields : [], values: rawSchema.values && typeof rawSchema.values === 'object' && !Array.isArray(rawSchema.values) ? rawSchema.values : {} } as ParsedSetYaml;
                const generator = { ...manifest, id: String(manifest.id), name: String(manifest.name ?? manifest.id), main_config: main,
                    fields: schema.fields ?? [], settings_files: {}, account_requirements: manifest.account_requirements ?? {}, config_folders: manifest.config_folders ?? [], enforces: manifest.enforces ?? [], configs: Array.isArray(manifest.configs) ? [...manifest.configs] : [],
                    default: { runner: { ...runner, settings: { [main]: effectiveValues(schema) } }, files: [] } } as unknown as RunnerGenerator;
                const configEntries = Array.isArray(manifest.configs) ? manifest.configs as { id: string; name?: string; path: string }[] : [];
                if (!configEntries.some(entry => entry.id === 'default')) generator.configs.unshift({ id: 'default', name: 'Default' });
                const generatorFolder = folderOf(definition.name);
                const configFiles = materialized.filter(file => file.name.startsWith(generatorFolder + '/configs/'));
                const configBodies = await fetchTextBodies(channel, cellId, configFiles);
                for (const entry of [...configEntries, ...(!configEntries.some(entry => entry.id === 'default') ? [{ id: 'default', name: 'Default', path: 'configs/default/config.set_yaml' }] : [])]) {
                    const configFile = materialized.find(file => file.name === `${generatorFolder}/${entry.path}`);
                    if (!configFile && entry.id !== 'default') throw new Error(`Config ${entry.id} is missing.`);
                    const config = configFile ? parseDefinition(configBodies.get(configFile.name) ?? configFile.content) : {};
                    const prefix = `${generatorFolder}/${folderOf(entry.path)}/files/`;
                    const companions=materialized.filter(file=>file.name.startsWith(prefix)).map(file=>({name:file.name.slice(prefix.length),content:file.content,language:file.language,...(file.encoding==='binary'?{blob_hash:file.body_hash!}:{})}));
                    const base = { ...generator.default.runner, ...config } as RunnerTemplate['runner'];
                    if (entry.id === 'default') base.settings = { [main]: effectiveValues(schema) };
                    configs[`${generator.id}:${entry.id}`] = { runner: base, files: companions };
                }
                const index = catalog.findIndex(item => item.id === generator.id);
                if (index >= 0) catalog[index] = generator; else catalog.push(generator);
            }
            if (alive) { setDraft({ runner, files: companions }); setGenerators(catalog); setLocalConfigs(configs); setError(''); }
        })().catch(e => { if (alive) setError(e instanceof Error ? e.message : String(e)); });
        return () => { alive = false; };
    }, [content, path, channel, cellId, files, folder, reset]);
    const save = async (value: RunnerDraft) => {
        if (!onUpdateFiles || readOnly) return;
        const additions: VirtualFile[] = [textFile(path, definitionText(value.runner as unknown as Record<string, unknown>))];
        for (const file of value.files) {
            const name = `${folder}/files/${file.name}`;
            if (file.encoding === 'base64') {
                const bytes = Uint8Array.from(atob(file.content), char => char.charCodeAt(0));
                const { blob_hash } = await uploadBlob(bytes);
                additions.push(binaryFile(name,blob_hash));
            } else additions.push(file.blob_hash?binaryFile(name,file.blob_hash,file.language):{...textFile(name,file.content),language:file.language??'text'});
        }
        const original = files.filter(file => file.name === path || file.name.startsWith(folder + '/files/'));
        const removed = new Set(original.filter(file => file.name !== path).map(file => file.name));
        await onUpdateFiles(current => {
            for (const file of original) {
                const latest = current.find(item => item.name === file.name);
                if (!latest || latest.file_revision !== file.file_revision || latest.body_hash !== file.body_hash) throw new Error('The runner package changed while you were saving. Reload it and retry.');
            }
            for (const file of additions) if (!original.some(item => item.name === file.name) && current.some(item => item.name === file.name)) throw new Error('A new companion file conflicts with this save. Reload it and retry.');
            return replaceFiles(current, additions, removed);
        });
    };
    return <div className="h-full overflow-auto">{error && <p role="alert" className={cn(ALERT, 'px-3 pt-3')}>{error}</p>}{draft && <fieldset disabled={readOnly} className="m-0 min-w-0 border-0 p-0"><RunnerEditor key={`${path}:${content}`} initial={draft} generators={generators} loadConfig={async (id, config) => localConfigs[`${id}:${config}`] ?? await fetchRunnerConfig(id, config)} onSave={save} onCancel={() => setReset(value => value + 1)} /></fieldset>}</div>;
}
