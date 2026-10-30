import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { FileText, Settings2, Shield, SlidersHorizontal, X } from 'lucide-react';
import { FieldControl, disabledOptionValues } from './FieldControl';
import { RunnerAccountField } from './RunnerAccountField';
import { computeVisibility, normalizeOptions } from '@/lib/setYaml';
import { fetchRunnerConfig, fetchRunnerGenerators, fetchRunnerVocabulary, type RunnerVocabulary, newSlugId, promptGate, type RunnerDraft, type RunnerGenerator, type RunnerTemplate } from '@/lib/runnerApi';
import { accountSettings, fetchAccounts, type Account } from '@/lib/accountsApi';
import { runnerReadiness } from '@/lib/runnerReadiness';
import { KeyManagerOverlay } from '@/components/settings/KeyManager';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { trapModalKeys, useModalFocus } from '@/lib/modalKeys';
import { btn, btnPrimary, card, chip, dialog, formInput, formLabel, formSelect, formTextarea, sectionLabel, tabOff, tabOn } from '@/components/ui/formStyles';

const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const TABS = [
    { id: 'config', label: 'Config', Icon: Settings2 },
    { id: 'advanced', label: 'Advanced', Icon: SlidersHorizontal },
    { id: 'files', label: 'Files', Icon: FileText },
    { id: 'security', label: 'Security', Icon: Shield },
] as const;
type Tab = typeof TABS[number]['id'];
// Commands is enforced by generators that declare it; the host serves all
// levels, omission defaults and display labels through the vocabulary API.
const securityAxes: { axis: 'docker' | 'internet' | 'commands'; help?: string; generatorAxis?: true }[] = [
    { axis: 'docker' }, { axis: 'internet' },
    { axis: 'commands', generatorAxis: true,
        help: 'Whether this generator may run shell commands. ask requests approval; allow runs commands unattended.' },
];
// An allowed-host entry is a bare host name the egress proxy matches by DNS
// suffix; one carrying a wildcard, scheme, path, port or inner space never
// matches (the SDK linter's runner-internet-allow-entry warning).
const neverMatchingHosts = (hosts: string[]): string[] => hosts.map(h => h.trim()).filter(h => /[*/:\s]/.test(h));
function parseStructuredSetting(text: string, kind: 'object' | 'list'): { value: Record<string, unknown> | unknown[] | null; error: string | null } {
    try {
        const value: unknown = text.trim() ? JSON.parse(text) : null;
        if (value !== null && (kind === 'list' ? !Array.isArray(value) : typeof value !== 'object' || Array.isArray(value))) throw new Error('Invalid structure');
        return { value: value as Record<string, unknown> | unknown[] | null, error: null };
    } catch { return { value: null, error: `Enter a valid JSON ${kind === 'list' ? 'array' : 'object'}, or leave this blank.` }; }
}

export interface RunnerEditorProps {
    initial?: RunnerDraft;
    generators?: RunnerGenerator[];
    loadConfig?: (generator: string, config: string) => Promise<RunnerTemplate>;
    onSave: (draft: RunnerDraft) => Promise<void> | void;
    onCancel: () => void;
}

/** A runner is bound to a generator first; everything after that — its name,
 *  fields, files, connections, permissions — stays in this draft until the
 *  host commits it. The Capabilities manage view and a .runner file's own
 *  viewer both edit through this form. */
export function RunnerEditor({ initial, generators: supplied, loadConfig, onSave, onCancel }: RunnerEditorProps) {
    const [generators, setGenerators] = useState<RunnerGenerator[]>(supplied ?? []);
    const [draft, setDraft] = useState<RunnerDraft | null>(() => initial ? copy({ runner: initial.runner, files: initial.files, revision: initial.revision }) : null);
    const [startGenerator, setStartGenerator] = useState('');
    const [tab, setTab] = useState<Tab>('config');
    const [vocabulary, setVocabulary] = useState<RunnerVocabulary | null>(null);
    const [accounts, setAccounts] = useState<Account[]>([]);
    const [keysOpen, setKeysOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [structuredText, setStructuredText] = useState<Record<string, string>>({});
    const [fileName, setFileName] = useState('');
    const [tagText, setTagText] = useState('');
    const [loadOpen, setLoadOpen] = useState(false);
    const [loadChoice, setLoadChoice] = useState('');
    const loadDialog = useRef<HTMLDivElement>(null);
    // The modal must OWN the keyboard while it is up: its host closes on
    // Escape, and an Escape that leaks there takes the whole draft with it.
    useModalFocus(loadDialog, loadOpen, 'input, button');
    useEffect(() => {
        let live = true;
        if (!supplied) void fetchRunnerGenerators().then(data => { if (live) setGenerators(data); }).catch(e => { if (live) setError(String(e.message)); });
        return () => { live = false; };
    }, [supplied]);
    useEffect(() => {
        let live = true;
        if (!keysOpen) void fetchAccounts().then(data => { if (live) setAccounts(data.accounts); }).catch(e => { if (live) setError(String(e.message)); });
        return () => { live = false; };
    }, [keysOpen]);
    useEffect(() => {
        let live = true;
        void fetchRunnerVocabulary().then(data => { if (live) setVocabulary(data); })
            .catch(e => { if (live) setError(String(e.message)); });
        return () => { live = false; };
    }, []);
    const generator = generators.find(g => g.id === draft?.runner.generator);
    const settingsFiles = new Set(Object.keys(generator?.settings_files ?? {}));
    if (generator) settingsFiles.add(generator.main_config);
    const values = draft && generator ? accountSettings(generator, draft.runner.accounts, accounts, draft.runner.settings[generator.main_config] ?? {}) : {};
    const fields = (generator?.fields ?? []).map(f => ({ ...f, options: normalizeOptions(f.options) }));
    const visible = computeVisibility(fields, values);
    const gate = generator ? promptGate(generator, values) : { shown: false, required: false };
    const invalidStructure = fields.some(f => (f.type === 'object' || f.type === 'list') && visible[f.key] !== false
        && !!parseStructuredSetting(structuredText[f.key] ?? (values[f.key] == null ? '' : JSON.stringify(values[f.key])), f.type as 'object' | 'list').error);
    const types = Object.fromEntries(fields.map(f => [f.key, f.type]));
    const update = (patch: Partial<RunnerDraft['runner']>) => setDraft(prev => prev ? { ...prev, runner: { ...prev.runner, ...patch } } : prev);
    const normalizedDraft = (): RunnerDraft => {
        const d = draft!;
        const p = d.runner.permissions;
        return { ...d, revision: initial?.revision ?? d.revision, runner: {
            ...d.runner, id: d.runner.id || newSlugId(d.runner.name.trim()), tags: d.runner.tags.map(t => t.trim()).filter(Boolean),
            settings: generator ? { ...d.runner.settings, [generator.main_config]: values } : d.runner.settings,
            folders: (generator?.config_folders ?? []).filter(folder => d.files.some(file => file.name.startsWith(`${folder.name}/`))).map(folder => folder.name),
            // The hosts textarea keeps raw lines while editing; trim them here.
            permissions: p?.internet_allow ? { ...p, internet_allow: p.internet_allow.map(s => s.trim()).filter(Boolean) } : p,
        } };
    };
    const start = () => {
        const g = generators.find(item => item.id === startGenerator);
        if (!g) return;
        // The draft opens nameless; its id is slugged from the name it is
        // saved with.
        setDraft({ runner: { ...copy(g.default.runner), id: '', generator: g.id, name: '' }, files: copy(g.default.files) });
        setStructuredText({}); setTab('config'); setError(null);
    };
    const confirmLoad = async () => {
        if (!draft || !generator || !loadChoice) return;
        setBusy(true); setError(null);
        try {
            const template = await (loadConfig ? loadConfig(generator.id, loadChoice) : fetchRunnerConfig(generator.id, loadChoice));
            setDraft({ ...draft, files: [
                ...draft.files.filter(file => settingsFiles.has(file.name)),
                ...copy(template.files.filter(file => !settingsFiles.has(file.name))),
            ] });
            setLoadOpen(false); setLoadChoice('');
        } catch (e) { setError(e instanceof Error ? e.message : 'Could not load file set'); }
        finally { setBusy(false); }
    };
    const save = async () => {
        if (!draft || invalidStructure) return;
        setBusy(true); setError(null);
        try { await onSave(normalizedDraft()); }
        catch (e) { setError(e instanceof Error ? e.message : 'Could not save runner'); }
        finally { setBusy(false); }
    };
    const addTags = (raws: string[]) => {
        setDraft(prev => {
            if (!prev) return prev;
            const tags = [...prev.runner.tags];
            for (const raw of raws) { const tag = raw.trim(); if (tag && !tags.includes(tag)) tags.push(tag); }
            return tags.length === prev.runner.tags.length ? prev : { ...prev, runner: { ...prev.runner, tags } };
        });
        setTagText('');
    };
    const addTag = (raw: string) => addTags([raw]);
    const addFile = () => {
        const name = fileName.trim();
        if (!draft || !name) return;
        if (name === generator?.main_config || name === 'prompt.md' || name.startsWith('/') || name.split('/').includes('..') || draft.files.some(f => f.name === name)) {
            setError('Choose a unique relative file path outside the main config and prompt.'); return;
        }
        setDraft({ ...draft, files: [...draft.files, { name, content: '', language: 'text' }] }); setFileName(''); setError(null);
    };
    const uploadFiles = async (incoming: FileList | null, replacementPath?: string) => {
        if (!draft || !incoming?.length) return;
        setBusy(true); setError(null);
        try {
            const additions = await Promise.all(Array.from(incoming).map(async file => {
                const name = replacementPath ?? (file.webkitRelativePath || file.name);
                if (name === generator?.main_config || name === 'prompt.md' || name.startsWith('/') || name.split('/').includes('..')) throw new Error('The main config and prompt are edited through their fields.');
                const bytes = new Uint8Array(await file.arrayBuffer());
                try {
                    const content = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
                    if (content.includes('\0')) throw new Error('binary');
                    return { name, content };
                } catch {
                    let binary = '';
                    for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
                    return { name, content: btoa(binary), encoding: 'base64' as const };
                }
            }));
            const paths = new Set(additions.map(file => file.name));
            setDraft({ ...draft, files: [...draft.files.filter(file => !paths.has(file.name)), ...additions] });
        } catch (e) { setError(e instanceof Error ? e.message : 'Could not read files'); }
        finally { setBusy(false); }
    };
    const renderFields = (advanced: boolean) => fields.filter(f => visible[f.key] !== false && (advanced ? f.advanced === true : f.advanced !== true)).map(field => {
        if (field.account_role) {
            const label = generator?.account_requirements[field.account_role]?.label ?? field.account_role;
            return <div key={field.key} className="flex items-center justify-between gap-3 rounded-md border border-teal/30 bg-teal-soft px-2.5 py-1.5 text-[0.75rem]">
                <span className="text-ink-2">{field.label || field.key}</span>
                <span className="text-right font-mono text-[0.75rem]">{draft?.runner.accounts[field.account_role] ? <>{String(values[field.key] ?? field.default ?? '')}<span className="ml-2 text-teal">· from {label}</span></> : <span className="text-teal">Choose {label}</span>}</span>
            </div>;
        }
        const setValue = (value: unknown) => update({ settings: { ...draft!.runner.settings, [generator!.main_config]: { ...values, [field.key]: value } } });
        if (field.type === 'object' || field.type === 'list') {
            const text = structuredText[field.key] ?? (values[field.key] == null ? '' : JSON.stringify(values[field.key], null, 2));
            const issue = parseStructuredSetting(text, field.type).error;
            return <label key={field.key} className="flex flex-col gap-1"><span className={formLabel}>{field.label || field.key}{field.required ? ' *' : ''}</span>
                <textarea aria-label={field.label || field.key} aria-invalid={!!issue} className={formTextarea} rows={5} value={text} disabled={busy}
                    onChange={event => {
                        const raw = event.target.value;
                        setStructuredText(previous => ({ ...previous, [field.key]: raw }));
                        const parsed = parseStructuredSetting(raw, field.type as 'object' | 'list');
                        if (!parsed.error) setValue(parsed.value);
                    }} />
                <span className={`text-[0.71875rem] ${issue ? 'text-red' : 'text-ink-3'}`}>{issue ?? (field.type === 'list' ? 'JSON array' : 'JSON object')}</span>
            </label>;
        }
        return <FieldControl key={field.key} field={field} value={values[field.key]} disabled={busy}
            disabledOptions={disabledOptionValues(field, values, visible, types)} onChange={setValue} />;
    });
    const permissions = draft?.runner.permissions ?? {};
    const requirements = Object.entries(generator?.account_requirements ?? {});
    const readiness = draft && generator ? runnerReadiness(draft, generator, accounts) : { missing: [], invalid: [] };
    const missing = [...readiness.missing, ...readiness.invalid];

    if (!draft) return <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4 text-ink" data-runner-editor>
        {error && <p role="alert" className="mb-3 text-[0.75rem] text-red">{error}</p>}
        <section className={`${card} flex flex-col gap-3 p-3`} aria-label="New runner">
            <label className="flex flex-col gap-1"><span className={formLabel}>Generator</span>
                <select className={formSelect} aria-label="Generator" autoFocus value={startGenerator} onChange={e => setStartGenerator(e.target.value)}>
                    <option value="">Choose a generator…</option>
                    {[...generators].sort((a, b) => a.name.localeCompare(b.name)).map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
                {generators.length === 0 && <span className="text-[0.71875rem] text-ink-3">No generators available.</span>}
            </label>
            <div className="flex justify-end gap-2 border-t border-line pt-3">
                <button className={btn} onClick={onCancel}>Cancel</button>
                <button className={btnPrimary} disabled={!startGenerator} onClick={start}>Next</button>
            </div>
        </section>
    </div>;

    return <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4 text-ink" data-runner-editor>
        {error && <p role="alert" className="mb-3 text-[0.75rem] text-red">{error}</p>}
        <fieldset disabled={busy} className="flex min-w-0 flex-col gap-3">
            <div className="flex items-center justify-between gap-2">
                <span className={sectionLabel}>Generator · {generator?.name ?? draft.runner.generator}</span>
            </div>
            <label className="flex flex-col gap-1"><span className={formLabel}>Name</span><input aria-label="Name" className={formInput} value={draft.runner.name} onChange={e => update({ name: e.target.value })} /></label>
            <label className="flex flex-col gap-1"><span className={formLabel}>Description</span><textarea aria-label="Description" className={formTextarea} rows={2} value={draft.runner.description} onChange={e => update({ description: e.target.value })} /></label>
            <div className="flex flex-col gap-1">
                <span className={formLabel}>Tags</span>
                <div className="flex min-h-[1.875rem] flex-wrap items-center gap-1.5 rounded-md border border-line bg-sheet px-2 py-1 transition-colors focus-within:border-line-2">
                    {draft.runner.tags.map(tag => <span key={tag} className={chip}>
                        {tag}<button type="button" aria-label={`Remove tag ${tag}`} className="text-ink-3 hover:text-red" onClick={() => update({ tags: draft.runner.tags.filter(t => t !== tag) })}><X className="h-3 w-3" /></button>
                    </span>)}
                    <input aria-label="Tags" className="min-w-[10ch] flex-1 bg-transparent text-[0.8125rem] font-mono text-ink placeholder:text-ink-3 outline-none" placeholder="Add a tag" value={tagText}
                        onChange={e => {
                            const raw = e.target.value;
                            const parts = raw.split(',');
                            if (parts.length > 1) { addTags(parts.slice(0, -1)); setTagText(parts.at(-1)!.trimStart()); } else setTagText(raw);
                        }}
                        onKeyDown={e => {
                            if (e.key === 'Enter') { e.preventDefault(); addTag(tagText); }
                            if (e.key === 'Backspace' && !tagText && draft.runner.tags.length) update({ tags: draft.runner.tags.slice(0, -1) });
                        }} />
                </div>
            </div>

            {/* The mock's tab strip: 30px, the 18px tabs on its top line. */}
            <div className="flex h-[1.875rem] flex-none items-start border-b border-line" role="tablist">
                {TABS.map(({ id, label, Icon }) => <button key={id} type="button" role="tab" id={`runner-tab-${id}`} aria-selected={tab === id} aria-controls={`runner-panel-${id}`}
                    className={`mt-px inline-flex h-[1.125rem] items-center gap-1.5 ${tab === id ? tabOn : tabOff}`}
                    onClick={() => setTab(id)}><Icon className="size-3.5" />{label}</button>)}
            </div>

            {tab === 'config' && <div id="runner-panel-config" role="tabpanel" tabIndex={0} aria-labelledby="runner-tab-config" className="flex flex-col gap-3 pt-1">
                {requirements.map(([role, requirement]) => <div key={role} className={`${card} p-2.5`}>
                    <RunnerAccountField role={role} requirement={requirement} accounts={accounts} value={draft.runner.accounts[role] ?? ''}
                        disabled={busy} onChange={value => {
                            const bindings = { ...draft.runner.accounts };
                            if (value) bindings[role] = value;
                            else delete bindings[role];
                            update({ accounts: bindings });
                        }} onOpenKeys={() => setKeysOpen(true)} />
                </div>)}
                {renderFields(false)}
                {gate.shown && <label className="flex flex-col gap-1"><span className={formLabel}>Prompt{gate.required ? ' *' : ''}</span>
                    {generator?.prompt_intent && <span className="text-[0.71875rem] text-ink-3">{generator.prompt_intent}</span>}
                    <textarea aria-label="Prompt" rows={4} className={formTextarea} value={draft.runner.prompt} onChange={e => update({ prompt: e.target.value })} />
                </label>}
            </div>}

            {tab === 'advanced' && <div id="runner-panel-advanced" role="tabpanel" tabIndex={0} aria-labelledby="runner-tab-advanced" className="flex flex-col gap-3 pt-1">
                {renderFields(true)}
                {!fields.some(f => f.advanced === true && visible[f.key] !== false) && <p className="text-[0.78125rem] text-ink-3">No advanced settings for this generator.</p>}
            </div>}

            {tab === 'files' && <div id="runner-panel-files" role="tabpanel" tabIndex={0} aria-labelledby="runner-tab-files" className="flex flex-col gap-3 pt-1">
                <div className="flex items-center justify-between gap-3">
                    <p className="text-[0.75rem] text-ink-3">Custom files supplied to this generator.</p>
                    <button type="button" className={btn} onClick={() => { setLoadChoice(''); setLoadOpen(true); }}>Load file sets</button>
                </div>
                {draft.files.filter(file => !settingsFiles.has(file.name)).map(file => <div className={`${card} p-2.5`} key={file.name}>
                    <div className="mb-2 flex items-center justify-between gap-2"><span className="break-all text-[0.75rem] font-mono">{file.name}</span><button type="button" className={btn} onClick={() => setDraft({ ...draft, files: draft.files.filter(f => f.name !== file.name) })}>Delete file</button></div>
                    <label className="mb-2 flex flex-col gap-1 text-[0.71875rem] text-ink-3">Replace file<input aria-label={`Replace ${file.name}`} type="file" onChange={e => void uploadFiles(e.target.files, file.name)} /></label>
                    {file.encoding === 'base64' || file.blob_hash ? <span className="text-[0.71875rem] text-ink-3">Binary file</span> : <textarea aria-label={`Contents of ${file.name}`} rows={6} className={formTextarea} value={file.content} onChange={e => setDraft({ ...draft, files: draft.files.map(f => f.name === file.name ? { ...f, content: e.target.value } : f) })} />}
                </div>)}
                <div className="flex gap-2"><input aria-label="New file path" className={formInput} value={fileName} onChange={e => setFileName(e.target.value)} placeholder="File path" /><button type="button" className={btn} onClick={addFile}>Add file</button></div>
                <label className="flex flex-col gap-1 text-[0.71875rem] text-ink-3">Upload or replace files<input type="file" multiple onChange={e => void uploadFiles(e.target.files)} /></label>
            </div>}

            {tab === 'security' && !vocabulary && <p>Loading permissions…</p>}
            {tab === 'security' && vocabulary && <div id="runner-panel-security" role="tabpanel" tabIndex={0} aria-labelledby="runner-tab-security" className="flex flex-col gap-3 pt-1">
                {securityAxes.map(({ axis, help, generatorAxis }) => {
                    const label = vocabulary.labels[axis];
                    const value = permissions[axis] ?? vocabulary.defaults[axis];
                    const description = vocabulary.descriptions[axis]?.[value];
                    if (generatorAxis && !generator?.enforces.includes(axis)) {
                        // A saved level this generator does not enforce has no
                        // effect; say so rather than hide it.
                        return generator && permissions[axis] !== undefined
                            ? <p key={axis} role="note" className="text-[0.75rem] text-ink-2">{label}: {permissions[axis]} — not enforced by this generator, so it has no effect.</p>
                            : null;
                    }
                    return <label key={axis} className="flex flex-col gap-1"><span className={formLabel}>{label}</span>
                        <select className={formSelect} aria-label={label} aria-describedby={description ? `runner-${axis}-description` : undefined} value={value} onChange={e => update({ permissions: { ...permissions, [axis]: e.target.value } })}>{vocabulary.enums[axis]?.map(value => <option key={value}>{value}</option>)}</select>
                        {description && <span id={`runner-${axis}-description`} className="text-[0.71875rem] text-ink-3">{description}</span>}
                        {help && <span className="text-[0.71875rem] text-ink-3">{help}</span>}
                        {axis === 'commands' && (permissions[axis] ?? vocabulary.defaults[axis]) === 'allow' && fields
                            .filter(field => field.type === 'dropdown' && visible[field.key] !== false)
                            .map(field => {
                                const selected = field.options?.find(option => option.value === String(values[field.key] ?? field.default ?? ''));
                                return selected?.description ? <span key={field.key} role="note" className="text-[0.75rem] text-ink-2">
                                    {field.label || field.key}: {selected.label}. {selected.description}
                                </span> : null;
                            })}
                    </label>;
                })}
                {(['update_packages'] as const).map(axis => <label key={axis} className="flex items-center gap-2 text-[0.75rem]"><input type="checkbox" className="accent-teal" checked={permissions[axis] ?? vocabulary.defaults[axis]} onChange={e => update({ permissions: { ...permissions, [axis]: e.target.checked } })} />{vocabulary.labels[axis]}</label>)}
                <label className="flex flex-col gap-1"><span className={formLabel}>{vocabulary.labels.internet_allow}</span><textarea aria-label={vocabulary.labels.internet_allow} className={formTextarea} rows={3} value={(permissions.internet_allow ?? vocabulary.defaults.internet_allow).join('\n')} onChange={e => update({ permissions: { ...permissions, internet_allow: e.target.value.split('\n') } })} />
                    <span className="text-[0.71875rem] text-ink-3">One host per line, used when Internet access is proxy. A host also allows its subdomains: example.com allows api.example.com.</span>
                </label>
                {neverMatchingHosts(permissions.internet_allow ?? []).length > 0 && <p role="note" className="text-[0.75rem] text-red">These entries never match: {neverMatchingHosts(permissions.internet_allow ?? vocabulary.defaults.internet_allow).join(', ')}. Write a bare host name, without wildcards, schemes, paths or ports.</p>}
            </div>}
        </fieldset>

        {invalidStructure && <p role="alert" className="mt-3 text-[0.75rem] text-red">Fix invalid structured settings before saving.</p>}
        {missing.length > 0 && <p role="status" className="mt-3 text-[0.75rem] text-red">This runner saves, but cannot run until you set: {missing.join(', ')}.</p>}
        <div className="mt-4 flex items-center justify-end gap-2 border-t border-line pt-3">
            <button className={btn} disabled={busy} onClick={onCancel}>Cancel</button>
            <button className={btnPrimary} disabled={busy || invalidStructure || !draft.runner.name.trim()} onClick={() => void save()}>Save</button>
        </div>

        {loadOpen && generator && createPortal(<div ref={loadDialog} className="fixed inset-0 z-[500] flex items-center justify-center bg-(--lift) p-4" role="dialog" aria-modal="true" aria-label="Load file sets" {...KEYBOARD_OWNER_PROPS}
            onKeyDown={e => trapModalKeys(e, loadDialog.current, () => setLoadOpen(false))}>
            <div className={`${dialog} flex w-full max-w-md flex-col gap-3 p-4`}>
                <h3 className="text-[0.84375rem] font-semibold text-ink">Load file sets</h3>
                <div className="flex max-h-64 flex-col gap-1 overflow-y-auto">
                    {generator.configs.map(config => <label key={config.id} className="flex h-[1.875rem] items-center gap-2 rounded-md px-2.5 text-[0.8125rem] text-ink-2 hover:bg-sheet-2 hover:text-ink">
                        <input type="radio" name="runner-file-set" className="accent-teal" checked={loadChoice === config.id} onChange={() => setLoadChoice(config.id)} />{config.name}
                    </label>)}
                    {!generator.configs.length && <p className="text-[0.78125rem] text-ink-3">This generator ships no file sets.</p>}
                </div>
                <p className="text-[0.75rem] text-red">Loading a file set replaces this runner's custom files.</p>
                <div className="flex justify-end gap-2 border-t border-line pt-3">
                    <button className={btn} onClick={() => setLoadOpen(false)}>Cancel</button>
                    <button className={btnPrimary} disabled={busy || !loadChoice} onClick={() => void confirmLoad()}>Confirm</button>
                </div>
            </div>
        </div>, document.body)}
        {keysOpen && <KeyManagerOverlay onClose={() => setKeysOpen(false)} />}
    </div>;
}
