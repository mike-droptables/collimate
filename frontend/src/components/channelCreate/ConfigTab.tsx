import { useEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { Download } from 'lucide-react';
import { fetchChannelOutline, fetchChannelTags, type ChannelOutline, type OutlineItem } from '@/lib/api/cells';
import { fetchChannelSettings } from '@/lib/api/settings';
import { expandZip, type ExportTarget, type ZipExpansion } from '@/lib/api/transfer';
import { parseSettingsImport, saveJsonFile, serializeSettings, settingsFileName } from '@/lib/settingsTransfer';
import { exportFileName, exportZip } from '@/lib/transfer/exportSinks';
import { isJsonFile, isZipFile, pickJson, pickZip } from '@/lib/transfer/importSources';
import { btn, formInput, formLabel, formSelect, sectionLabel } from '@/components/ui/formStyles';
import { isAbortError } from '@/lib/utils';
import { historyFromZip, mainFromZip, packagesFromZip, plural, type ZipRead } from './createBody';
import { SourceSection } from './SourceSection';
import { SECTION_LABELS, type ChannelChoice, type ConfigSectionKey, type ConfigState, type Fetched, type PickedFile, type SourceMode } from './state';

type ZipSection = 'packages' | 'main' | 'history';
type CacheKey = 'outlines' | 'tags' | 'settingsDocs';
type CacheValue<K extends CacheKey> = ConfigState[K][string] extends Fetched<infer T> | undefined ? T : never;

const ZIP_READERS: Record<ZipSection, (expansion: ZipExpansion) => ZipRead<unknown>> = {
    packages: packagesFromZip,
    main: mainFromZip,
    history: historyFromZip,
};

const LOADERS: { [K in CacheKey]: (channel: string) => Promise<CacheValue<K>> } = {
    outlines: fetchChannelOutline,
    tags: fetchChannelTags,
    settingsDocs: fetchChannelSettings,
};

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

interface Props {
    config: ConfigState;
    setConfig: Dispatch<SetStateAction<ConfigState>>;
    channels: ChannelChoice[];
}

/**
 * The Config tab: four sections, each None, copied from an existing channel
 * (its latest files, like an export) or read from a file. What another
 * channel holds is fetched by its id when a section needs it, once: the
 * answers live in the page's state. A zip upload is cancelled when its
 * section takes another file or leaves From file, and when the page closes.
 */
export function ConfigTab({ config, setConfig, channels }: Props) {
    const { packages, settings, main, history } = config;
    const [exportError, setExportError] = useState('');
    const channelName = (id: string) => channels.find(channel => channel.id === id)?.name ?? '';

    // In-flight fetches, so a render before the answer asks no second time.
    const pending = useRef(new Set<string>());
    useEffect(() => {
        const want = <K extends CacheKey>(cache: K, id: string) => {
            const key = `${cache}:${id}`;
            if (!id || config[cache][id] || pending.current.has(key)) return;
            pending.current.add(key);
            void LOADERS[cache](id).then(
                value => ({ state: 'ready' as const, value }),
                error => ({ state: 'failed' as const, error: message(error, 'Could not read that channel.') }),
            ).then(result => {
                pending.current.delete(key);
                setConfig(prev => ({ ...prev, [cache]: { ...prev[cache], [id]: result } }));
            });
        };
        if (packages.mode === 'channel' && packages.expanded) want('outlines', packages.channelId);
        if (main.mode === 'channel') want('outlines', main.channelId);
        if (history.mode === 'channel') want('tags', history.channelId);
        if (settings.mode === 'channel') want('settingsDocs', settings.channelId);
    }, [config, packages, main, history, settings, setConfig]);

    // The zip upload each section has under way (a Map that lives as long
    // as the tab, read only from handlers).
    const [uploads] = useState(() => new Map<ConfigSectionKey, AbortController>());
    useEffect(() => () => uploads.forEach(controller => controller.abort()), [uploads]);
    /** Cancel a section's upload; its own rejection says so on the file line. */
    const cancelUpload = (key: ConfigSectionKey) => {
        uploads.get(key)?.abort();
        uploads.delete(key);
    };

    const setSection = <K extends ConfigSectionKey>(key: K, patch: Partial<ConfigState[K]>) =>
        setConfig(prev => ({ ...prev, [key]: { ...prev[key], ...patch } }));
    /** A new file for a section: whatever it was uploading gives way. */
    const setFile = (key: ConfigSectionKey, file: PickedFile<unknown>) => {
        cancelUpload(key);
        setConfig(prev => ({ ...prev, [key]: { ...prev[key], file } }));
    };
    /** Set a section's file only while `expected` is still the one it
     *  shows: a newer pick wins over an older upload. */
    const settle = (key: ConfigSectionKey, expected: PickedFile<unknown>, next: PickedFile<unknown>, extra: Partial<ConfigState['history']> = {}) =>
        setConfig(prev => prev[key].file === expected ? { ...prev, [key]: { ...prev[key], ...extra, file: next } } : prev);

    const readZip = (key: ZipSection, file: File) => {
        if (!isZipFile(file)) { setFile(key, { status: 'refused', name: file.name, error: 'Not a zip file.' }); return; }
        const reading: PickedFile<unknown> = { status: 'reading', name: file.name };
        setFile(key, reading);
        const controller = new AbortController();
        uploads.set(key, controller);
        const done = () => { if (uploads.get(key) === controller) uploads.delete(key); };
        void expandZip(file, { signal: controller.signal }).then(expansion => {
            done();
            const read = ZIP_READERS[key](expansion);
            if (!read.ok) { settle(key, reading, { status: 'refused', name: file.name, error: read.error }); return; }
            const extra = key === 'history' ? { fileTag: (read.value as { root: string }).root } : {};
            settle(key, reading, { status: 'ready', name: file.name, value: read.value, summary: read.summary }, extra);
        }, error => {
            done();
            settle(key, reading, { status: 'refused', name: file.name, error: isAbortError(error) ? 'The upload was cancelled.' : message(error, 'The zip could not be read.') });
        });
    };
    const readJson = (file: File) => {
        if (!isJsonFile(file)) { setFile('settings', { status: 'refused', name: file.name, error: 'Not a JSON file.' }); return; }
        const reading: PickedFile<unknown> = { status: 'reading', name: file.name };
        setFile('settings', reading);
        void file.text().then(text => {
            const parsed = parseSettingsImport(text);
            settle('settings', reading, parsed.ok
                ? { status: 'ready', name: file.name, value: parsed.doc, summary: 'a channel settings document' }
                : { status: 'refused', name: file.name, error: parsed.error });
        }, error => settle('settings', reading, { status: 'refused', name: file.name, error: message(error, 'The file could not be read.') }));
    };
    const dropped = (key: ConfigSectionKey, files: File[]) => {
        if (files.length !== 1) { setFile(key, { status: 'refused', name: '', error: 'Drop one file.' }); return; }
        if (key === 'settings') readJson(files[0]); else readZip(key, files[0]);
    };
    // The pickers open inside the click: nothing is awaited before them.
    const pickZipFor = (key: ZipSection) => {
        void pickZip().then(file => { if (file) readZip(key, file); }).catch(e => setFile(key, { status: 'refused', name: '', error: message(e, 'The picker failed.') }));
    };
    const pickSettings = () => {
        void pickJson().then(file => { if (file) readJson(file); }).catch(e => setFile('settings', { status: 'refused', name: '', error: message(e, 'The picker failed.') }));
    };
    /** Save a channel's export under the server's download name:
     *  `<channel> packages`, `<channel> main cells`, `<channel> <tag> cells`. */
    const exportOf = (channelId: string, target: ExportTarget) => {
        setExportError('');
        const channel = exportFileName(channelName(channelId), 'Channel');
        const name = target.scope === 'packages' ? `${channel} packages`
            : target.scope === 'main' ? `${channel} main cells`
                : target.scope === 'tag' ? `${channel} ${exportFileName(target.tag, 'Tag')} cells` : channel;
        // exportZip opens its save picker before its first await.
        void exportZip(channelId, target, name).catch(e => setExportError(message(e, 'The export failed.')));
    };
    const exportSettings = (doc: Parameters<typeof serializeSettings>[0]) => {
        setExportError('');
        void saveJsonFile(settingsFileName(channelName(settings.channelId)), serializeSettings(doc)).catch(e => setExportError(message(e, 'The export failed.')));
    };

    const common = (key: ConfigSectionKey) => ({
        label: SECTION_LABELS[key],
        mode: config[key].mode,
        onMode: (mode: SourceMode) => {
            if (mode !== 'file') cancelUpload(key);
            setSection(key, { mode });
        },
        channels,
        channelId: config[key].channelId,
        file: config[key].file,
        onDrop: (files: File[]) => dropped(key, files),
    });

    const outlineOf = (id: string) => config.outlines[id];
    const settingsDoc = config.settingsDocs[settings.channelId];
    const tags = config.tags[history.channelId];

    return <div className="flex flex-col gap-3">
        <p className="text-[0.78125rem] text-ink-2">Each part starts empty, comes from an existing channel, or from a file. Cells arrive with their latest files only, like an export, without their version history.</p>
        {exportError && <p role="alert" className="text-[0.75rem] text-red">{exportError}</p>}

        <SourceSection {...common('packages')} onChannel={id => setSection('packages', { channelId: id })}
            pickLabel="Choose zip…" onPick={() => pickZipFor('packages')}
            channelBody={<>
                <div className="flex flex-wrap items-center gap-2">
                    <button type="button" className={btn} aria-expanded={packages.expanded} onClick={() => setSection('packages', { expanded: !packages.expanded })}>Capabilities cells</button>
                    <ExportButton onClick={() => exportOf(packages.channelId, { scope: 'packages' })} />
                </div>
                <p className="text-[0.71875rem] text-ink-3">Built-in examples and regenerated skills stay in this channel.</p>
                {packages.expanded && <Fetching fetched={outlineOf(packages.channelId)}>{outline =>
                    <NameList heading="Capabilities" items={outline.capabilities_cells} />}</Fetching>}
            </>} />

        <SourceSection {...common('settings')} onChannel={id => setSection('settings', { channelId: id })}
            pickLabel="Choose JSON…" onPick={pickSettings}
            channelBody={<div className="flex flex-wrap items-center gap-2">
                <ExportButton label="Export JSON" disabled={settingsDoc?.state !== 'ready'}
                    onClick={() => { if (settingsDoc?.state === 'ready') exportSettings(settingsDoc.value); }} />
                {settingsDoc?.state === 'failed' && <span role="alert" className="text-[0.75rem] text-red">{settingsDoc.error}</span>}
            </div>} />

        <SourceSection {...common('main')} onChannel={id => setSection('main', { channelId: id })}
            pickLabel="Choose zip…" onPick={() => pickZipFor('main')}
            channelBody={<>
                <Fetching fetched={outlineOf(main.channelId)}>{outline => <StackList outline={outline} />}</Fetching>
                <div><ExportButton onClick={() => exportOf(main.channelId, { scope: 'main' })} /></div>
            </>} />

        <SourceSection {...common('history')} onChannel={id => setSection('history', { channelId: id, tag: '' })}
            pickLabel="Choose zip…" onPick={() => pickZipFor('history')}
            channelBody={<Fetching fetched={tags}>{list => list.length ? <div className="flex flex-wrap items-center gap-2">
                <select aria-label="Tag" className={`${formSelect} w-auto min-w-[14rem] flex-1`} value={history.tag} onChange={e => setSection('history', { tag: e.target.value })}>
                    <option value="">Choose a tag…</option>
                    {list.map(item => <option key={item.tag} value={item.tag}>{item.tag} · {plural(item.live + item.removed, 'cell')}</option>)}
                </select>
                <ExportButton disabled={!history.tag} onClick={() => exportOf(history.channelId, { scope: 'tag', tag: history.tag })} />
            </div> : <p className="text-[0.75rem] text-ink-3">No tags</p>}</Fetching>}
            fileBody={history.file?.status === 'ready' && <label className="flex flex-col gap-1">
                <span className={formLabel}>Tag (empty leaves the cells untagged)</span>
                <input aria-label="Tag" className={formInput} value={history.fileTag} onChange={e => setSection('history', { fileTag: e.target.value })} />
            </label>} />
        <p className="text-[0.75rem] text-ink-3">History cells land on the new channel's Removed shelf, ready to restore.</p>
    </div>;
}

function ExportButton({ label = 'Export zip', disabled, onClick }: { label?: string; disabled?: boolean; onClick: () => void }) {
    return <button type="button" className={btn} disabled={disabled} onClick={onClick}><Download className="size-[0.8125rem]" />{label}</button>;
}

/** A fetched answer's body, its failure, or a loading line meanwhile. */
function Fetching<T>({ fetched, children }: { fetched: Fetched<T> | undefined; children: (value: T) => ReactNode }) {
    if (!fetched) return <p className="text-[0.75rem] text-ink-3">Loading…</p>;
    if (fetched.state === 'failed') return <p role="alert" className="text-[0.75rem] text-red">{fetched.error}</p>;
    return <>{children(fetched.value)}</>;
}

function NameList({ heading, items }: { heading: string; items: OutlineItem[] }) {
    return <div className="flex min-w-0 flex-col gap-1">
        <h4 className={sectionLabel}>{heading}</h4>
        {items.length ? <ul className="flex flex-col text-[0.78125rem] text-ink-2">{items.map(item => <li key={item.id} className="truncate">{item.name}</li>)}</ul>
            : <p className="text-[0.75rem] text-ink-3">None</p>}
    </div>;
}

function StackList({ outline }: { outline: ChannelOutline }) {
    if (!outline.main_stacks.length) return <p className="text-[0.75rem] text-ink-3">No main cells</p>;
    return <ul aria-label="Main stacks" className="flex flex-col gap-1.5">
        {outline.main_stacks.map(stack => <li key={stack.id} className="flex flex-col gap-0.5">
            <span className="text-[0.78125rem] font-medium text-ink">{stack.name || 'Untitled stack'}</span>
            <ul className="flex flex-col border-l border-line pl-2.5 text-[0.78125rem] text-ink-2">
                {stack.cells.map(cell => <li key={cell.id} className="truncate">{cell.name || 'Untitled cell'}</li>)}
            </ul>
        </li>)}
    </ul>;
}
