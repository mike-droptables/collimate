import { useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type Ref } from 'react';
import { Download, FileArchive, FileUp, FolderDown, FolderUp } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { commitImport, expandZip, type ExportTarget, type ImportStack } from '@/lib/api/transfer';
import { uploadAll, type QueueProgress, type UploadedBlob } from '@/lib/transfer/uploadQueue';
import { splitLevels, type SplitLevel, type SplitLevelId } from '@/lib/transfer/splitLevels';
import { isZipFile, pickFiles, pickFolder, pickZip, treeFromDrop, type PickedTree } from '@/lib/transfer/importSources';
import { exportFileName, exportFolder, exportZip, folderExportAvailability, type ExportFormat, type ExportProgress } from '@/lib/transfer/exportSinks';
import { captureDrop, collectDroppedFiles, hasDroppedItems } from '@/lib/dropEntries';
import { carriesOsFiles } from '@/lib/fileDrag';
import { mergeByName } from '@/lib/fileTransfer';
import { folderMarkersFor } from '@/lib/fileIngest';
import { isReservedCellPath } from '@/lib/transfer/cellPaths';
import { languageForPath } from '@/lib/fileLanguage';
import { binaryFile } from '@/lib/fileCommands';
import { formatBytes } from '@/lib/fileInfo';
import type { VirtualFile } from '@/lib/types';
import { btn, btnPrimary, formLabel, sectionLabel } from '@/components/ui/formStyles';
import { cn, isAbortError } from '@/lib/utils';
import { CellPanel, type CellPanelPrimary, type CellPanelRow } from './CellPanel';

const MODES = [
    { id: 'import', label: 'Import' },
    { id: 'export', label: 'Export' },
] as const;
type Mode = typeof MODES[number]['id'];

type ImportRowId = 'files' | 'folder' | 'zip';
type ExportRowId = 'cell' | 'stack' | 'all';

/** A file of an import: on disk still (a pick or a drop), or already in the
 *  CAS (a zip the server expanded). */
interface SourceFile {
    path: string;
    file?: File;
    stored?: UploadedBlob;
}

/** What the Folder and Zip rows (and a drop on the panel) bring to the
 *  step: a tree relative to its root. */
interface ImportSource {
    rootName: string;
    /** A folder of the user's keeps its name inside the cell; a zip's or a
     *  loose drop's members land as they are. */
    keepRoot: boolean;
    files: SourceFile[];
    dirs: string[];
    /** Members left out, with why (unreadable files, refused zip members). */
    skipped: { path: string; reason: string }[];
}

type Destination = 'folder' | 'cells';

/** A transfer under way: what it says, how far it is, and its cancel. */
interface Running {
    label: string;
    fraction: number | null;
    detail: string;
    controller: AbortController;
}

const quoted = (name: string) => `‘${name}’`;
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const STEP_SCROLL = 'flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 pb-3 pt-2.5';

/** The progress bar and its Cancel. A null fraction is a transfer of
 *  unknown length (a zip streaming to disk): the bar stays empty. */
function ProgressLine({ running }: { running: Running }) {
    const percent = running.fraction === null ? null : Math.round(Math.min(1, Math.max(0, running.fraction)) * 100);
    return <div data-testid="transfer-progress" className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-[0.78125rem] text-ink-2">{running.label}</span>
            <span className="flex-none text-[0.75rem] tabular-nums text-ink-3">{running.detail}</span>
            <button type="button" className={btn} onClick={() => running.controller.abort()}>Cancel</button>
        </div>
        <div role="progressbar" aria-label={running.label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}
            className="h-1 overflow-hidden rounded-full bg-sheet-3">
            <div className="h-full rounded-full bg-teal transition-[width]" style={{ width: `${percent ?? 0}%` }} />
        </div>
    </div>;
}

function Section({ label, children }: { label: string; children: ReactNode }) {
    return <section className="flex flex-col gap-1">
        <h4 className={sectionLabel}>{label}</h4>
        {children}
    </section>;
}

/** One radio choice of the step, with an optional greyed reason. */
function Choice({ name, checked, disabled, label, reason, onChange, inputRef }: {
    name: string; checked: boolean; disabled?: boolean; label: string; reason?: string; onChange: () => void; inputRef?: Ref<HTMLInputElement>;
}) {
    return <label className={cn('flex items-start gap-2 rounded-md px-1 py-[0.1875rem] text-[0.78125rem]', disabled ? 'text-ink-3' : 'cursor-pointer text-ink-2')}>
        <input ref={inputRef} type="radio" className="mt-[0.1875rem] accent-teal" name={name} checked={checked} disabled={disabled} onChange={onChange} />
        <span className="min-w-0">
            <span className={checked ? 'text-ink' : undefined}>{label}</span>
            {reason && <span className="block text-[0.75rem] text-ink-3">{reason}</span>}
        </span>
    </label>;
}

/** The preview: the stacks and cells an import creates, with file counts. */
function Preview({ level }: { level: SplitLevel }) {
    return <ul aria-label="Preview" className="flex flex-col gap-1.5">
        {level.preview.stacks.map((stack, i) => <li key={`${stack.name}-${i}`} className="flex flex-col gap-0.5">
            <span className="text-[0.78125rem] font-medium text-ink">{stack.name || 'Untitled stack'}</span>
            <ul className="flex flex-col border-l border-line pl-2.5">
                {stack.cells.map((cell, j) => <li key={`${cell.name}-${j}`} className="flex items-baseline gap-2 text-[0.78125rem] text-ink-2">
                    <span className="min-w-0 flex-1 truncate">{cell.name}</span>
                    <span className="flex-none text-[0.75rem] tabular-nums text-ink-3">{plural(cell.fileCount, 'file')}</span>
                </li>)}
            </ul>
        </li>)}
    </ul>;
}

/**
 * The transfer panel: the cell's panel of kind `transfer`, opened from the
 * Files pane's "Import / export" link. Import brings files from disk:
 * Files… lands the picked files in this cell's current folder; Folder… and
 * Zip… (and a folder or zip dropped on the panel) open a step that chooses
 * the destination (this cell's current folder, or new cells after this
 * stack), the split level (only the levels the tree allows) and shows the
 * stacks and cells it creates before anything is imported. Every file
 * streams into the CAS (lib/transfer/uploadQueue) and new cells are made in
 * one commit (POST /import). Export writes this cell, this stack or the
 * whole channel as plain folders: into a folder the user picks, or as one
 * zip; the secondary action (⇧↵) switches between the two.
 *
 * Picked files and folders cannot outlive a reload: a restored session
 * opens on its mode list.
 */
export function TransferPanel({ cellId, onClose }: { cellId: string; onClose: () => void }) {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const channelName = useCollimateStore(s => s.activeChannel?.name ?? '');
    const { located, stackId, cellName, stackName } = useCollimateStore(useShallow(s => {
        const loc = s.locateCell(cellId);
        const ch = loc && s.state?.channels.find(c => c.id === loc.channel);
        const stack = [...(ch?.main_stacks ?? []), ...(ch?.nav_stacks ?? [])].find(item => item.id === loc?.stackId);
        return { located: !!loc, stackId: loc?.stackId ?? '', cellName: loc?.cell.name ?? '', stackName: stack?.name ?? '' };
    }));
    const inHistory = useCollimateStore(s => !!s.cellHistory[cellId]);
    const currentFolder = useCollimateStore(s => s.cellLayouts[cellId]?.filesPane.currentFolder ?? '');

    const readOnlyReason = !located ? 'This cell is no longer available.'
        : inHistory ? 'Leave version history to import into this cell.'
            : undefined;
    const folderRefusal = readOnlyReason ?? (isReservedCellPath(currentFolder) ? 'The current folder belongs to a run; only the run writes there.' : undefined);
    const folderLabel = currentFolder ? quoted(currentFolder) : 'the cell’s root';

    const [chosenMode, setMode] = useState<Mode>('import');
    // Nothing is imported into a cell that cannot take it: the panel shows
    // Export only.
    const mode: Mode = readOnlyReason ? 'export' : chosenMode;
    const [importRow, setImportRow] = useState<ImportRowId>('files');
    const [exportRow, setExportRow] = useState<ExportRowId>('cell');
    const folderAvailability = useMemo(() => folderExportAvailability(), []);
    const [format, setFormat] = useState<ExportFormat>(folderAvailability.available ? 'folder' : 'zip');
    const [source, setSource] = useState<ImportSource | null>(null);
    const [destination, setDestination] = useState<Destination>('cells');
    const [level, setLevel] = useState<SplitLevelId>(0);
    const [running, setRunning] = useState<Running | null>(null);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [dragOver, setDragOver] = useState(false);

    // The transfer under way, read outside renders; one outliving the
    // panel stops with it.
    const live = useRef<Running | null>(null);
    useEffect(() => () => live.current?.controller.abort(), []);
    // A step opens with the keyboard on its first choice, so the arrows
    // move between choices, Enter imports and Esc goes back. This runs after
    // the panel's own focus of the step (a parent's effects run last).
    const firstChoice = useRef<HTMLInputElement>(null);
    useEffect(() => { if (source) firstChoice.current?.focus({ preventScroll: true }); }, [source]);

    const levels = useMemo(() => source ? splitLevels(source.files.map(f => f.path), source.dirs, source.rootName) : [], [source]);
    const chosenLevel = levels.find(l => l.level === level) ?? levels[0];

    const close = () => { live.current?.controller.abort(); onClose(); };

    /** Run one transfer: a fresh controller, its progress line, its failure
     *  in the error line; a cancel is no failure. */
    const run = async (label: string, work: (controller: AbortController, progress: (fraction: number | null, detail: string) => void) => Promise<void>) => {
        if (live.current) return;
        const controller = new AbortController();
        const start: Running = { label, fraction: 0, detail: '', controller };
        live.current = start;
        setRunning(start);
        setError(''); setNotice('');
        const progress = (fraction: number | null, detail: string) => setRunning(r => (r && r.controller === controller ? { ...r, fraction, detail } : r));
        try {
            await work(controller, progress);
        } catch (e) {
            if (!isAbortError(e) && !controller.signal.aborted) setError((e as Error)?.message || String(e));
        } finally {
            live.current = null;
            setRunning(r => (r?.controller === controller ? null : r));
        }
    };
    const uploadProgress = (progress: (fraction: number | null, detail: string) => void) => (p: QueueProgress) =>
        progress(p.totalBytes ? p.sentBytes / p.totalBytes : p.totalFiles ? p.doneFiles / p.totalFiles : null,
            `${p.doneFiles}/${p.totalFiles} · ${formatBytes(p.sentBytes)} of ${formatBytes(p.totalBytes)}`);

    /** Upload what still sits on disk; the stored blob of each path. */
    const uploadSource = async (files: SourceFile[], controller: AbortController, progress: (fraction: number | null, detail: string) => void) => {
        const pending = files.filter(f => !f.stored && f.file);
        const stored = await uploadAll(pending.map(f => ({ path: f.path, body: f.file! })), { signal: controller.signal, onProgress: uploadProgress(progress) });
        const all = new Map<string, UploadedBlob>(files.flatMap(f => (f.stored ? [[f.path, f.stored] as const] : [])));
        for (const [path, blob] of stored) all.set(path, blob);
        return all;
    };

    /** A path relative to the cell's current folder, relative to the cell. */
    const underFolder = (path: string) => (currentFolder ? `${currentFolder}/${path}` : path);

    /** Land files (paths relative to the cell's current folder) in this
     *  cell, merged over what it holds now. A file or folder that would land
     *  in a reserved band is left out (the step lists it). */
    const importIntoCell = async (picked: SourceFile[], dirs: string[], controller: AbortController, progress: (fraction: number | null, detail: string) => void) => {
        const files = picked.filter(f => !isReservedCellPath(underFolder(f.path)));
        const stored = await uploadSource(files, controller, progress);
        const additions: VirtualFile[] = files.flatMap(f => {
            const blob = stored.get(f.path);
            const name = underFolder(f.path);
            if (!blob) return [];
            return [blob.encoding === 'binary'
                ? binaryFile(name, blob.blob_hash)
                : { name, language: languageForPath(name), file_revision: 0, body_hash: blob.blob_hash, encoding: 'utf-8' as const, writer_id: '', edit_seq: 0 }];
        });
        if (controller.signal.aborted) return;
        const store = useCollimateStore.getState();
        const cell = store.locateCell(cellId)?.cell;
        if (!cell) throw new Error('This cell is no longer available.');
        const merged = mergeByName(cell.files, additions);
        const next = [...merged, ...folderMarkersFor(dirs.map(underFolder).filter(d => !isReservedCellPath(d)), merged)];
        if (!await store.applyCellFiles(cellId, next)) throw new Error('The files could not be saved into this cell. Retry.');
        onClose();
    };

    /** Create the level's stacks and cells after this cell's stack. Only
     *  the files the level places upload: what it leaves out never does. */
    const importAsCells = async (src: ImportSource, split: SplitLevel, controller: AbortController, progress: (fraction: number | null, detail: string) => void) => {
        const placed = new Set(split.stacks.flatMap(stack => stack.cells.flatMap(cell => cell.files.map(f => f.source))));
        const stored = await uploadSource(src.files.filter(f => placed.has(f.path)), controller, progress);
        if (controller.signal.aborted) return;
        const stacks: ImportStack[] = split.stacks.map(stack => ({
            name: stack.name,
            cells: stack.cells.map(cell => ({
                name: cell.name,
                files: cell.files.flatMap(f => { const blob = stored.get(f.source); return blob ? [{ path: f.path, blob_hash: blob.blob_hash }] : []; }),
                dirs: cell.dirs,
            })),
        }));
        const store = useCollimateStore.getState();
        const loc = store.locateCell(cellId);
        const mainStacks = (loc && store.state?.channels.find(c => c.id === loc.channel)?.main_stacks) ?? [];
        const at = loc ? mainStacks.findIndex(stack => stack.id === loc.stackId) : -1;
        progress(1, 'Creating cells…');
        const result = await commitImport(channel, { insert_index: at < 0 ? null : at + 1, stacks }, controller.signal);
        await store.fetchState();
        if (result.cell_ids[0]) store.requestSelectCell(result.cell_ids[0]);
        onClose();
    };

    const openSource = (src: ImportSource) => {
        setSource(src);
        setDestination('cells');
        setLevel(0);
        setError(''); setNotice('');
    };

    const treeSource = (tree: PickedTree): ImportSource => ({
        rootName: tree.rootName, keepRoot: tree.rootIsFolder, files: tree.files, dirs: tree.dirs,
        skipped: tree.unreadable.map(path => ({ path, reason: 'Could not be read' })),
    });

    const expand = (zip: File) => run(`Uploading ${quoted(zip.name)}`, async (controller, progress) => {
        const expansion = await expandZip(zip, {
            signal: controller.signal,
            onProgress: (sent, total) => progress(total ? sent / total : null, `${formatBytes(sent)} of ${formatBytes(total)}`),
        });
        openSource({
            rootName: zip.name.replace(/\.zip$/i, '') || 'Imported files',
            keepRoot: false,
            files: expansion.entries.map(entry => ({ path: entry.path, stored: { blob_hash: entry.blob_hash, encoding: entry.encoding } })),
            dirs: expansion.dirs,
            skipped: expansion.skipped,
        });
    });

    // The pickers open inside the key press or click that asked for them:
    // nothing is awaited before each one.
    const runImportRow = () => {
        if (importRow === 'files') {
            if (folderRefusal) return;
            void pickFiles().then(files => {
                if (!files?.length) return;
                return run(`Importing ${plural(files.length, 'file')} into ${folderLabel}`, (controller, progress) =>
                    importIntoCell(files.map(file => ({ path: file.name, file })), [], controller, progress));
            }).catch(e => setError((e as Error)?.message || String(e)));
            return;
        }
        if (importRow === 'folder') {
            void pickFolder().then(tree => { if (tree) openSource(treeSource(tree)); }).catch(e => setError((e as Error)?.message || String(e)));
            return;
        }
        void pickZip().then(zip => (zip ? expand(zip) : undefined)).catch(e => setError((e as Error)?.message || String(e)));
    };

    const runImportStep = () => {
        if (!source || running) return;
        if (destination === 'folder') {
            if (folderRefusal) return;
            const prefix = source.keepRoot ? `${source.rootName}/` : '';
            const files = source.files.map(f => ({ ...f, path: `${prefix}${f.path}` }));
            const dirs = source.dirs.map(d => `${prefix}${d}`);
            void run(`Importing ${plural(files.length, 'file')} into ${folderLabel}`, (controller, progress) => importIntoCell(files, dirs, controller, progress));
            return;
        }
        if (!chosenLevel) return;
        const cells = chosenLevel.stacks.reduce((n, stack) => n + stack.cells.length, 0);
        void run(`Importing ${plural(cells, 'cell')}`, (controller, progress) => importAsCells(source, chosenLevel, controller, progress));
    };

    // ── Export ─────────────────────────────────────────────────────────
    const exportTarget = (row: ExportRowId): ExportTarget | null => {
        if (row === 'cell') return { scope: 'cell', cellId };
        if (row === 'stack') return stackId ? { scope: 'stack', stackId } : null;
        return { scope: 'all' };
    };
    // The server's download name: the zip's root folder.
    const exportName = (row: ExportRowId) => row === 'cell' ? exportFileName(cellName, 'Cell')
        : row === 'stack' ? exportFileName(stackName, 'Stack') : exportFileName(channelName, 'Channel');
    const exportProgress = (progress: (fraction: number | null, detail: string) => void) => (p: ExportProgress) =>
        progress(p.totalBytes ? p.doneBytes / p.totalBytes : null,
            p.totalFiles === null ? formatBytes(p.doneBytes) : `${p.doneFiles}/${p.totalFiles} · ${formatBytes(p.doneBytes)}`);
    const runExport = () => {
        const target = exportTarget(exportRow);
        if (!target || running) return;
        const name = exportName(exportRow);
        const label = format === 'folder' ? `Exporting ${quoted(name)} to a folder` : `Exporting ${quoted(`${name}.zip`)}`;
        // run() reaches the export's picker before its first await, so the
        // picker opens inside this key press or click.
        void run(label, async (controller, progress) => {
            const options = { signal: controller.signal, onProgress: exportProgress(progress) };
            const outcome = await (format === 'folder' ? exportFolder(channel, target, options) : exportZip(channel, target, name, options));
            if (outcome === 'saved') setNotice(format === 'folder' ? `Exported ${quoted(name)} into the folder.` : `Saved ${quoted(`${name}.zip`)}.`);
            if (outcome === 'downloaded') setNotice(`Downloading ${quoted(`${name}.zip`)}.`);
        });
    };

    // ── Drop onto the panel ────────────────────────────────────────────
    const onDragOver = (e: ReactDragEvent) => {
        if (!carriesOsFiles(e.dataTransfer)) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = readOnlyReason || running ? 'none' : 'copy';
        setDragOver(true);
    };
    const onDragLeave = (e: ReactDragEvent) => {
        if (!(e.currentTarget as Node).contains(e.relatedTarget as Node | null)) setDragOver(false);
    };
    const onDrop = (e: ReactDragEvent) => {
        if (!carriesOsFiles(e.dataTransfer)) return;
        e.preventDefault();
        e.stopPropagation();
        setDragOver(false);
        // dataTransfer.items dies when this handler returns: snapshot now.
        const capture = captureDrop(e.dataTransfer);
        if (readOnlyReason) { setError(readOnlyReason); return; }
        if (running || !hasDroppedItems(capture)) return;
        void collectDroppedFiles(capture).then(drop => {
            if (drop.files.length === 1 && drop.emptyDirs.length === 0 && !drop.files[0].path.includes('/') && isZipFile(drop.files[0].file)) {
                return expand(drop.files[0].file);
            }
            if (drop.files.length === 0 && drop.emptyDirs.length === 0) return;
            setMode('import');
            openSource(treeSource(treeFromDrop(drop)));
        }).catch(err => setError((err as Error)?.message || String(err)));
    };

    // ── The list view ──────────────────────────────────────────────────
    const importRows: CellPanelRow[] = [
        { id: 'files', name: 'Files…', description: `Into ${folderLabel} of this cell`, disabled: !!folderRefusal, reason: folderRefusal },
        { id: 'folder', name: 'Folder…', description: 'A folder from disk: into this cell, or as new cells' },
        { id: 'zip', name: 'Zip…', description: 'A zip archive, unpacked on the server: into this cell, or as new cells' },
    ];
    const exportRows: CellPanelRow[] = [
        { id: 'cell', name: 'This cell', description: `${cellName || 'Untitled cell'}/ and its files` },
        { id: 'stack', name: 'This stack', description: `${stackName || 'Untitled stack'}/ with a folder per cell`, disabled: !located },
        { id: 'all', name: 'All cells and stacks', description: `${channelName || 'The channel'}/ with a folder per stack, Capabilities included` },
    ];
    const IMPORT_PRIMARY: Record<ImportRowId, Pick<CellPanelPrimary, 'label' | 'icon'>> = {
        files: { label: 'Choose files…', icon: FileUp },
        folder: { label: 'Choose folder…', icon: FolderUp },
        zip: { label: 'Choose zip…', icon: FileArchive },
    };
    const importRefused = readOnlyReason ?? (importRow === 'files' ? folderRefusal : undefined);
    const primary: CellPanelPrimary = mode === 'import'
        ? { ...IMPORT_PRIMARY[importRow], intent: 'run', disabled: !!running || !!importRefused, reason: importRefused, onRun: runImportRow }
        : {
            label: format === 'folder' ? 'Export to folder…' : 'Export as zip…', icon: format === 'folder' ? FolderDown : Download, intent: 'run',
            disabled: !!running || !exportTarget(exportRow), reason: running ? 'An export is under way' : undefined, onRun: runExport,
        };
    const secondary = mode === 'export'
        ? {
            label: format === 'folder' ? 'Switch to zip' : 'Switch to folder', hint: '⇧↵',
            disabled: !!running || (format === 'zip' && !folderAvailability.available),
            onRun: () => setFormat(f => (f === 'folder' ? 'zip' : 'folder')),
        }
        : undefined;
    const note = running ? <ProgressLine running={running} />
        : notice ? <span role="status">{notice}</span>
            : mode === 'export' && !folderAvailability.available ? <span>{folderAvailability.reason}</span>
                : dragOver ? <span>Drop files, a folder or a zip to import them.</span>
                    : undefined;

    // ── The step ───────────────────────────────────────────────────────
    let step: { body: ReactNode; onBack: () => void } | undefined;
    if (source) {
        const fileCount = source.files.length;
        const intoFolder = destination === 'folder';
        const prefix = source.keepRoot ? `${source.rootName}/` : '';
        // What the destination leaves out: paths that would land in a
        // reserved band (the import commit refuses them).
        const landsReserved = (path: string) => intoFolder && isReservedCellPath(underFolder(`${prefix}${path}`));
        const reservedFiles = source.files.map(f => f.path).filter(landsReserved);
        const reservedDirs = source.dirs.filter(landsReserved);
        const reserved = intoFolder ? [...reservedFiles, ...reservedDirs] : chosenLevel?.leftOut ?? [];
        const leftOut = [...source.skipped, ...reserved.map(path => ({ path, reason: 'Reserved for runs' }))];
        const landing = fileCount - reservedFiles.length;
        const ready = intoFolder
            ? !folderRefusal && (landing > 0 || source.dirs.length > reservedDirs.length)
            : !!chosenLevel && chosenLevel.stacks.length > 0;
        const onStepKey = (e: ReactKeyboardEvent) => {
            if (e.key !== 'Enter' || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
            if ((e.target as HTMLElement).closest('button')) return;
            e.preventDefault();
            if (ready) runImportStep();
        };
        step = {
            onBack: () => { if (!running) { setSource(null); setError(''); } else running.controller.abort(); },
            body: <div className="flex min-h-0 flex-1 flex-col" onKeyDown={onStepKey}>
                <div className="flex-none px-3 pb-1.5 pt-2.5 text-[0.8125rem] font-medium text-ink">
                    Import {quoted(source.rootName)} · {plural(fileCount, 'file')}
                    {source.dirs.length > 0 && <span className="font-normal text-ink-3"> · {plural(source.dirs.length, 'empty folder')}</span>}
                </div>
                {error && <p role="alert" className="mx-3 mb-1 flex-none text-[0.75rem] text-red">{error}</p>}
                <div className={STEP_SCROLL}>
                    <Section label="Destination">
                        <div role="radiogroup" aria-label="Destination" className="flex flex-col">
                            <Choice name="transfer-destination" inputRef={firstChoice} checked={!intoFolder} label="As new cells after this stack" onChange={() => setDestination('cells')} />
                            <Choice name="transfer-destination" checked={intoFolder} disabled={!!folderRefusal} reason={folderRefusal}
                                label={`Into ${folderLabel} of this cell`} onChange={() => setDestination('folder')} />
                        </div>
                    </Section>
                    {!intoFolder && levels.length > 0 && <Section label="Split">
                        <div role="radiogroup" aria-label="Split level" className="flex flex-col">
                            {levels.map(l => <Choice key={l.level} name="transfer-level" checked={chosenLevel?.level === l.level} label={l.label} onChange={() => setLevel(l.level)} />)}
                        </div>
                    </Section>}
                    <Section label="Preview">
                        {intoFolder
                            ? <p className="text-[0.78125rem] text-ink-2">
                                {plural(landing, 'file')} land in {source.keepRoot ? `${quoted(source.rootName)} inside ` : ''}{folderLabel}; a file of the same path is replaced.
                            </p>
                            : chosenLevel && chosenLevel.stacks.length > 0 ? <Preview level={chosenLevel} /> : <p className="text-[0.78125rem] text-ink-3">Nothing to import.</p>}
                    </Section>
                    {leftOut.length > 0 && <Section label="Left out">
                        <ul className="flex flex-col">
                            {leftOut.slice(0, 20).map((s, i) => <li key={`${s.path}-${i}`} className="text-[0.75rem] text-ink-3"><span className="font-mono">{s.path}</span> — {s.reason}</li>)}
                            {leftOut.length > 20 && <li className="text-[0.75rem] text-ink-3">and {leftOut.length - 20} more</li>}
                        </ul>
                    </Section>}
                </div>
                <div className="flex min-h-10 flex-none flex-col justify-center gap-2 border-t border-line px-3 py-2">
                    {running ? <ProgressLine running={running} /> : <div className="flex items-center justify-end gap-1.5">
                        <span className={cn(formLabel, 'mr-auto')}>{intoFolder ? `Into ${folderLabel}` : chosenLevel ? `${plural(chosenLevel.stacks.length, 'stack')}, ${plural(chosenLevel.stacks.reduce((n, s) => n + s.cells.length, 0), 'cell')}` : ''}</span>
                        <button type="button" className={btnPrimary} disabled={!ready} onClick={runImportStep}><FolderUp className="h-3.5 w-3.5" />Import</button>
                    </div>}
                </div>
            </div>,
        };
    }

    return <div className="contents" data-transfer-drop="" onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
        <CellPanel<Mode>
            cellId={cellId}
            kind="transfer"
            title="Import / export"
            note={note}
            modes={MODES.map(m => (m.id === 'import' && readOnlyReason ? { ...m, disabled: true, reason: readOnlyReason } : m))}
            mode={mode}
            onMode={next => { if (running) return; setMode(next); setError(''); setNotice(''); }}
            error={source ? undefined : error || undefined}
            rows={mode === 'import' ? importRows : exportRows}
            selectedId={mode === 'import' ? importRow : exportRow}
            onSelect={id => (mode === 'import' ? setImportRow(id as ImportRowId) : setExportRow(id as ExportRowId))}
            primary={primary}
            secondary={secondary}
            step={step}
            onClose={close}
        />
    </div>;
}
