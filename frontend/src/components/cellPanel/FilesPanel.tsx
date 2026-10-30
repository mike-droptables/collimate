import { useEffect, useMemo, useRef, useState, type Dispatch, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type Ref, type SetStateAction } from 'react';
import { Activity, Clock, Copy, Ellipsis, FilePlus, Folder, FolderPlus, PackagePlus, Pencil, Sparkles, type LucideIcon } from 'lucide-react';
import { isCapabilitiesStack, type Cell, type CapabilityKind, type VirtualFile } from '@/lib/types';
import type { CellLayout, FilesPanelFilters } from '@/stores/collimate/types';
import { folderOutsideRuns, runPathRefusal, type LockOwnerOf } from '@/lib/fileLocks';
import { isRunPath } from '@/lib/runningCell';
import { runDisplayName } from '@/lib/runLabels';
import { fetchTextBodies, withFetchedBodies, type UpdateFiles } from '@/lib/fileTransfer';
import { FOLDER_KEEP_FILE, isFolderKeep, remintEmptiedParent } from '@/lib/fileTree';
import { definitionKind, parseDefinition, replaceFiles } from '@/lib/definitionFiles';
import { addApprovedPackage, fetchHandlers, previewApproval, type ApprovalPreview, type ApprovalRequest, type HandlerInfo } from '@/lib/definitionApi';
import { offerBuiltinSkills } from '@/lib/api/capabilitiesStack';
import { fetchCellProvenance, fetchJournal, type CellProvenance, type JournalEvent } from '@/lib/api/cells';
import { revealHandler } from '@/lib/revealFile';
import { compileQuery, compareRecency } from '@/lib/fileSearch';
import { folderEntries, parentFolder, ROOT_LABEL } from '@/lib/folderBrowse';
import { eventLabel, fileEvents, fileSizeOf, firstChange, formatBytes, lastChange, type FileTime } from '@/lib/fileInfo';
import { filenameCompletions } from '@/lib/filenameCompletions';
import { findFileTab, setFilesPanelFilters } from '@/lib/cellLayout';
import { allRenderers, rendererCandidatesFor, resolveRenderer, resolveRendererWithOverride, useRendererId, useSupportedExtensions, type ResolvedRenderer } from '@/hooks/useRendererId';
import { useFileBodies } from '@/hooks/useFileBodies';
import { useFileOperations } from '@/hooks/useFileOperations';
import { useRendererPreviewHash } from '@/lib/rendererPreviewEvidence';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { flushFileDocuments } from '@/lib/fileDocuments';
import { handlerTypeForPath, handlerTypeRules } from '@/lib/handlerTypes';
import { focusCellCard } from '@/lib/commandPane';
import { fetchState } from '@/lib/api';
import { when } from '@/components/overview/journalText';
import { Switch } from '@/components/ui/switch';
import { btn, btnPrimary, chip, formInput, formLabel, sectionLabel } from '@/components/ui/formStyles';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cn } from '@/lib/utils';
import { CellPanel, type CellPanelHost, type CellPanelPrimary, type CellPanelRow } from './CellPanel';

const MODES = [
    { id: 'search', label: 'Search' },
    { id: 'browse', label: 'Browse/Create' },
] as const;
type Mode = typeof MODES[number]['id'];

/** Browse's two creation rows, always under the folder's entries. Their ids
 *  hold a NUL, which no path does. */
const NEW_FILE = '\u0000new-file';
const NEW_FOLDER = '\u0000new-folder';
type Creating = 'file' | 'folder';

const BUILTIN_REFUSAL = 'Built-in packages are part of the app';

/** How long a typed query waits before it is written into the session. */
const QUERY_WRITE_MS = 250;
/** How many of the journal's events naming the file Info lists. */
const RECENT_CHANGES = 8;

/** The steps a file opens. Each names the file it acts on, so a moved
 *  selection never retargets a step. */
type Step =
    | { kind: 'options'; path: string }
    | { kind: 'open-with'; path: string }
    | { kind: 'info'; path: string }
    | { kind: 'duplicate'; path: string }
    | { kind: 'rename'; path: string }
    | { kind: 'approval'; path: string; request: ApprovalRequest; preview: ApprovalPreview }
    | { kind: 'skills'; path: string; handlerId: string; name: string };

/** What the list's note says happened, with a way to the result in
 *  Capabilities when there is one. */
interface Notice { text: string; reveal?: () => void }

const basename = (path: string) => path.slice(path.lastIndexOf('/') + 1);
const quoted = (path: string) => `‘${basename(path)}’`;
/** `name` inside `folder` ('' = the root). */
const within = (folder: string, name: string) => (folder ? `${folder}/${name}` : name);
/** A file name's stem and its suffix with the dot ('' without one). */
const splitSuffix = (name: string): [string, string] => {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, ''];
};
/** The file's suffix: what a handler claims. */
const suffix = (path: string) => splitSuffix(basename(path))[1];
const rendererLabel = (renderer: ResolvedRenderer) => renderer.name || renderer.id;

/** The files a search keeps, in the panel's order: the active tab's file
 *  first, then the tree's selected file, then path order; Recent orders
 *  them newest first instead. Runs keeps only the files a live run locks.
 *  Folder-keep markers are no files of the user's. */
function panelFiles(
    files: readonly VirtualFile[], query: string, filters: FilesPanelFilters,
    activePath: string | null, selectedPath: string | null, lockOwnerOf: LockOwnerOf,
): VirtualFile[] {
    const matches = compileQuery(query);
    const kept = files.filter(file => !isFolderKeep(file.name) && matches(file.name)
        && (!filters.runsOnly || !!lockOwnerOf(file.name)));
    if (filters.sortByRecency) {
        return kept.sort((a, b) => compareRecency({ path: a.name, timestamp: a.timestamp }, { path: b.name, timestamp: b.timestamp }));
    }
    const rank = (path: string) => (path === activePath ? 0 : path === selectedPath ? 1 : 2);
    return kept.sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
}

/** Why a live run's lock holds `path`, as the files pane says it. */
function lockRefusal(path: string, lockOwnerOf: LockOwnerOf): string | undefined {
    const owner = lockOwnerOf(path);
    return owner ? `Held by ${runDisplayName(owner)} until that run stops` : undefined;
}

/** Why the file cannot be duplicated, renamed or deleted, or undefined when
 *  it can: a built-in card's files are the app's, and a live run holds the
 *  files it locks. */
function changeRefusal(path: string, lockOwnerOf: LockOwnerOf, builtin: boolean): string | undefined {
    return builtin ? BUILTIN_REFUSAL : lockRefusal(path, lockOwnerOf);
}

/** A free path for a copy of `path`, beside it: `name-copy.ext`, then
 *  `name-copy-2.ext` and on. A run's file copies out of its run folder. */
function copyPath(path: string, taken: ReadonlySet<string>): string {
    const folder = folderOutsideRuns(parentFolder(path));
    const [stem, ext] = splitSuffix(basename(path));
    const at = (n: number) => within(folder, `${stem}-copy${n > 1 ? `-${n}` : ''}${ext}`);
    let n = 1;
    while (taken.has(at(n))) n++;
    return at(n);
}

/** Why `name` cannot be a copy's path, or undefined when it can: a
 *  relative path with no empty, `.` or `..` segment, outside run folders,
 *  not already a file of the cell's. */
function duplicateRefusal(name: string, taken: ReadonlySet<string>): string | undefined {
    if (!name) return 'Name the copy.';
    if (name.startsWith('/') || name.includes('\\') || name.split('/').some(part => !part || part === '.' || part === '..')) {
        return 'Choose a relative path with no empty, . or .. segment.';
    }
    if (isRunPath(name)) return 'Choose a path outside run folders.';
    if (taken.has(name)) return `‘${name}’ already exists.`;
    return undefined;
}

/** Why `name` cannot be the file's new name beside it, or undefined when it
 *  can: one segment, not reserved, no file or folder of that path already,
 *  outside run folders, and the file not held by a live run. */
function renameRefusal(name: string, path: string, files: readonly VirtualFile[], lockOwnerOf: LockOwnerOf): string | undefined {
    if (!name) return 'Name the file.';
    if (name.includes('/') || name.includes('\\')) return 'A name can’t contain /';
    if (name === '.' || name === '..') return 'Invalid name';
    if (name === FOLDER_KEEP_FILE) return 'Reserved name';
    const target = within(parentFolder(path), name);
    if (files.some(file => file.name === target || file.name.startsWith(`${target}/`))) return `‘${target}’ already exists.`;
    if (isRunPath(target)) return 'Choose a path outside run folders.';
    return lockRefusal(path, lockOwnerOf);
}

/** A definition file's id and name, as row tags (none for an unloaded or
 *  malformed one: it stays openable). */
function definitionTags(file: VirtualFile): string[] {
    const meta = definitionMeta(file);
    return meta ? [...new Set([meta.id, meta.name].filter(value => value !== undefined && value !== null && value !== '').map(String))] : [];
}
/** A loaded definition file's parsed head, or null (unloaded, malformed). */
function definitionMeta(file: VirtualFile): Record<string, unknown> | null {
    if (!definitionKind(file.name) || !('content' in file)) return null;
    try { return parseDefinition(String(file.content)); } catch { return null; }
}
/** What a package is called in the approval: its definition's name, else
 *  its id, else the file's stem. */
function packageName(file: VirtualFile): string {
    const meta = definitionMeta(file);
    const named = [meta?.name, meta?.id].find(value => typeof value === 'string' && value !== '');
    return typeof named === 'string' ? named : splitSuffix(basename(file.name))[0];
}

/** A definition of a capability (an options file configures its own cell
 *  and is no capability). */
const isCapabilityKind = (kind: ReturnType<typeof definitionKind>): kind is CapabilityKind => !!kind && kind !== 'options';
/** The paths of the skills a built-in handler's package regenerated:
 *  `<folder>/derived/skills/**`. */
const regeneratedSkills = (files: readonly VirtualFile[], folder: string) =>
    files.map(file => file.name).filter(name => name.startsWith(`${folder}/derived/skills/`) && !isFolderKeep(name)).sort();

/** A time the journal gives for a file, hedged when it is only a bound:
 *  `bound` says which ("On or before" an arrival, "On or after" a change). */
const fileTime = (time: FileTime, bound: string) => (time.exact ? when(time.t) : `${bound} ${when(time.t)}`);

/** The fields that name an entry of a summary list (a handler type's
 *  `ext`, an account's `name`), first found first. */
const ENTRY_NAME_KEYS = ['ext', 'name', 'id', 'folder', 'path', 'key'] as const;

/** A summary value as one line of text: a list as its entries, an entry
 *  object as its name with its other fields after it (a handler's types
 *  read '.note, .mview (render_as: .md)'), a mapping as 'key: value' pairs. */
function plain(value: unknown): string {
    if (Array.isArray(value)) return value.length ? value.map(plainEntry).join(', ') : 'none';
    if (value !== null && typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}: ${plainEntry(item)}`).join('; ');
    return String(value);
}
function plainEntry(value: unknown): string {
    if (Array.isArray(value)) return plain(value);
    if (value === null || typeof value !== 'object') return String(value);
    const record = value as Record<string, unknown>;
    const nameKey = ENTRY_NAME_KEYS.find(key => typeof record[key] === 'string' || typeof record[key] === 'number');
    const rest = Object.entries(record).filter(([key]) => key !== nameKey);
    const restText = rest.map(([key, item]) => `${key}: ${plainEntry(item)}`).join(', ');
    if (!nameKey) return `(${restText})`;
    return rest.length ? `${String(record[nameKey])} (${restText})` : String(record[nameKey]);
}

const STEP_ROW = 'block w-full rounded-md px-2 py-[0.4375rem] text-left transition-colors hover:bg-sheet-2';
/** An entry of a step's list: ↑↓ select it, ↵ or a click runs it. */
const OPTION_ROW = 'block w-full rounded-md px-2 py-[0.4375rem] text-left transition-colors aria-selected:bg-sheet-2 hover:bg-sheet-2';
const STEP_NOTE = 'px-2 text-[0.78125rem] text-ink-3';
const MONO = 'font-mono text-[0.71875rem] leading-[1.45] text-ink-2 break-all';

/** A step's frame: its title, the failure (role=alert), a scrolling body
 *  and an optional footer with the step's action. */
function StepFrame({ title, error, footer, children }: { title: string; error: string; footer?: ReactNode; children: ReactNode }) {
    return <>
        <div className="flex-none px-3 pb-1.5 pt-2.5 text-[0.8125rem] font-medium text-ink">{title}</div>
        {error && <p role="alert" className="mx-3 mb-1 flex-none text-[0.75rem] text-red">{error}</p>}
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 pb-3">{children}</div>
        {footer && <div className="flex h-10 flex-none items-center justify-end gap-1.5 border-t border-line pl-3 pr-2">{footer}</div>}
    </>;
}

/** A labelled group inside a step (the mock's `h4`). */
function Section({ label, children }: { label: string; children: ReactNode }) {
    return <section role="group" aria-label={label} className="flex flex-col gap-1">
        <h4 className={sectionLabel}>{label}</h4>
        {children}
    </section>;
}

/** Key/value lines (a file's facts, a package's identity and hashes, its
 *  summary). */
function Facts({ rows }: { rows: readonly (readonly [string, ReactNode])[] }) {
    return <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1 text-[0.78125rem]">
        {rows.map(([key, value]) => <div key={key} className="contents">
            <dt className="text-ink-3">{key}</dt>
            <dd className="min-w-0 text-ink-2">{value}</dd>
        </div>)}
    </dl>;
}

/** An entry of a step's list (the options, Open with): a row, under its
 *  section when it has one, greyed with `refusal` when the file cannot
 *  take it. */
interface ListEntry {
    id: string;
    section?: string;
    label: string;
    icon?: LucideIcon;
    description?: string;
    refusal?: string;
}

/** One entry of the options step's flat list: `action` on the file. An
 *  entry that opens a step is named after it, so Back from the step lands
 *  on it. */
interface OptionEntry extends ListEntry {
    action: Exclude<Step['kind'], 'options'> | 'open' | 'delete';
    section?: undefined;
    /** The Use these skills step the entry opens. */
    skills?: Extract<Step, { kind: 'skills' }>;
}

/** One entry of the Open with step: a renderer, opened with `rendererId`
 *  (none for the file's default, which clears a tab's override). */
interface RendererEntry extends ListEntry {
    section: 'For this file' | 'Other renderers';
    rendererId: string | undefined;
}

/** A step's keyboard list (the options, Open with): one listbox named
 *  `title`, its entries in order (under their sections when they have
 *  them), `index` selected; ↑↓ move it and wrap, ↵ or a click runs the selected entry. */
function StepList<E extends ListEntry>({ ref, cellId, title, entries, index, onIndex, onRun }: {
    ref: Ref<HTMLDivElement>; cellId: string; title: string; entries: E[];
    index: number; onIndex: Dispatch<SetStateAction<number>>; onRun: (entry: E) => void;
}) {
    const current = entries[Math.min(index, entries.length - 1)];
    const row = (entry: E) =>
        <div key={entry.id} id={`${cellId}-option-${entry.id}`} role="option" aria-selected={entry === current} aria-disabled={!!entry.refusal || undefined}
            className={cn(OPTION_ROW, entry.refusal ? 'cursor-default' : 'cursor-pointer')}
            onMouseDown={e => e.preventDefault()}
            // The second press of the double-click that opened
            // this step lands here: only a press of its own runs.
            onClick={e => { if (e.detail > 1) return; onIndex(entries.indexOf(entry)); onRun(entry); }}>
            <span className={cn('flex items-center gap-2 font-medium', entry.refusal ? 'text-ink-3' : 'text-ink')}>
                {entry.icon && <entry.icon className="h-3.5 w-3.5 flex-none text-ink-3" aria-hidden />}
                {entry.label}
            </span>
            {entry.description && <span className="mt-px block text-[0.75rem] leading-[1.4] text-ink-3">{entry.description}</span>}
            {entry.refusal && <span className="mt-px block text-[0.75rem] leading-[1.4] text-ink-3">{entry.refusal}</span>}
        </div>;
    return <div ref={ref} role="listbox" aria-label={title} tabIndex={-1} aria-activedescendant={current ? `${cellId}-option-${current.id}` : undefined}
        className="flex flex-col gap-3 outline-none"
        onKeyDown={e => {
            if (e.altKey || e.ctrlKey || e.metaKey) return;
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                onIndex(at => (Math.min(at, entries.length - 1) + (e.key === 'ArrowDown' ? 1 : entries.length - 1)) % entries.length);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                onRun(current);
            }
        }}>
        {[...new Set(entries.map(entry => entry.section))].map(section => {
            const rows = entries.filter(entry => entry.section === section).map(row);
            return section === undefined ? <div key="" className="flex flex-col gap-1">{rows}</div> : <Section key={section} label={section}>{rows}</Section>;
        })}
    </div>;
}

export interface FilesPanelProps {
    cell: Cell;
    channel: string;
    /** The cell's layout: the tree selection, the Runs / Recent toggles and
     *  the file's open tab (its Open with renderer). */
    layout: CellLayout;
    onLayoutChange: (fn: (layout: CellLayout) => CellLayout) => void;
    /** The focused tab's file, listed first. */
    activePath: string | null;
    lockOwnerOf: LockOwnerOf;
    /** The cell's mounted file writer (create, duplicate, rename, delete). */
    onUpdateFiles: UpdateFiles;
    onOpenFile: (path: string) => void;
    /** Open with a renderer (CellEditor's handleOpenFileWith). */
    onOpenFileWith: (path: string, rendererId: string | undefined) => void;
    /** In its cell unless the cell editor hands it to the Commands pane. */
    host?: CellPanelHost;
}

/**
 * The Files panel (row P03): the cell's panel of kind `files`, opened by
 * the files rail's Find file, Mod+P and the empty tab. Two modes:
 *
 * - Search lists the cell's files under their paths (glob or substring
 *   through lib/fileSearch; the Runs and Recent toggles persist per cell in
 *   the layout, the query in the session).
 * - Browse/Create walks the folders from the root (↵ or a click enters a
 *   folder, Esc goes up one, and closes at the root; the field filters the
 *   folder's names) and always offers New file here and New folder here,
 *   which turn the field into the new entry's name (a new file's extension
 *   completes as in the small file creator).
 *
 * ↵ or a click on a file opens its options: Add to Capabilities (a
 * generator, runner, handler or renderer definition in a cell off
 * Capabilities; for a handler with a built-in handler's id, Use these
 * skills, which offers its regenerated skills to that built-in), Open
 * (with the renderer its tab shows), Open with… (a step: the renderers
 * that claim the file, then every other renderer), Info (the file's facts,
 * the handler that claims it, its recent changes in the cell's journal),
 * Duplicate, Rename and Delete (an inline confirm). An option the file
 * cannot take is greyed with its reason.
 */
export function FilesPanel({ cell, channel, layout, onLayoutChange, activePath, lockOwnerOf, onUpdateFiles, onOpenFile, onOpenFileWith, host }: FilesPanelProps) {
    const cellId = String(cell.id);
    // A built-in card's files only open and inform.
    const builtin = !!cell.builtin;
    // A cell on Capabilities already offers what it holds: nothing to add.
    const onCapabilities = useCollimateStore(s => {
        const ch = s.activeChannel;
        return isCapabilitiesStack([...(ch?.main_stacks ?? []), ...(ch?.nav_stacks ?? [])].find(stack => stack.cells.some(c => String(c.id) === cellId)));
    });
    const { files: definitionBodies } = useFileBodies(channel, cellId, cell.files.filter(file => !!definitionKind(file.name)));
    const closeSession = useCollimateStore(s => s.closePickSession);
    const sessionQuery = useCollimateStore(s => {
        const session = s.pickSessions[cellId];
        return session?.kind === 'files' ? session.query : '';
    });
    const [mode, setMode] = useState<Mode>('search');
    // The field's text: Search's query (the session's), or Browse's filter.
    const [query, setQuery] = useState(sessionQuery);
    // Browse's folder ('' = the root).
    const [folder, setFolder] = useState('');
    // What a creation row makes while the field names it, and the name.
    const [creating, setCreating] = useState<Creating | null>(null);
    const [newName, setNewName] = useState('');
    // A new file's extension completion, as the small file creator cycles
    // and accepts it.
    const [completionIndex, setCompletionIndex] = useState(0);
    const [completionAccepted, setCompletionAccepted] = useState(false);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [step, setStep] = useState<Step | null>(null);
    const [confirming, setConfirming] = useState<string | null>(null);
    const [error, setError] = useState('');
    const [notice, setNoticeState] = useState<Notice | null>(null);
    const setNotice = (text: string, reveal?: () => void) => setNoticeState(text ? { text, reveal } : null);
    const [busy, setBusy] = useState(false);
    // The handler catalog: null until it arrives; a failed load is its own
    // message, so Info never passes a missing catalog off as "no handler".
    const [handlers, setHandlers] = useState<HandlerInfo[] | null>(null);
    const [handlersError, setHandlersError] = useState('');
    const builtinHandlerIds = useMemo(() => new Set((handlers ?? []).filter(h => h.builtin).map(h => h.id)), [handlers]);
    const [replace, setReplace] = useState(false);
    // The Duplicate and Rename steps' name field.
    const [stepName, setStepName] = useState('');
    const nameInput = useRef<HTMLInputElement>(null);
    // The selected entry of a step's list, the top one when the step opens.
    const [optionIndex, setOptionIndex] = useState(0);
    // What Info loads as it opens: the file's size (null while loading) and
    // a page of the cell's journal.
    const [size, setSize] = useState<number | 'unavailable' | null>(null);
    const [journal, setJournal] = useState<JournalEvent[] | null>(null);
    const [provenance, setProvenance] = useState<CellProvenance | string | null>(null);
    // A step's list (the options, Open with), Info's body or the approval:
    // what takes the keyboard when its step comes up.
    const stepFocus = useRef<HTMLDivElement>(null);
    // Bumped by every step change and on unmount: async work that finishes
    // after it changes nothing on screen.
    const generation = useRef(0);
    const pendingQuery = useRef<string | null>(null);
    const queryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    // Set by each write of the file operations: a rename the cell's "Save
    // your edits and rename?" called off has written nothing.
    const wrote = useRef(false);
    const { handleCreateFileAtPath, handleCreateFolder, handleRename } = useFileOperations({
        files: cell.files, readOnly: builtin, lockOwnerOf, channel, cellId,
        onUpdateFiles: (update, options) => { wrote.current = true; return onUpdateFiles(update, options); },
    });
    const extensions = useSupportedExtensions();
    // The hint names the renderer of the name as typed: what ↵ creates now.
    const { renderer: newRenderer, error: newRendererError } = useRendererId(creating === 'file' ? newName.trim() : '');

    const browsing = mode === 'browse';
    const filters = layout.filesPane.panelFilters;
    const treeSelection = layout.filesPane.selection;
    const selectedPath = treeSelection && !treeSelection.isFolder ? treeSelection.nodePath : null;
    const files = useMemo(
        () => (browsing ? [] : panelFiles(cell.files, query, filters, activePath, selectedPath, lockOwnerOf)),
        [browsing, cell.files, query, filters, activePath, selectedPath, lockOwnerOf],
    );
    const entries = useMemo(() => (browsing ? folderEntries(cell.files, folder, query) : []), [browsing, cell.files, folder, query]);
    const approvalPath = step?.kind === 'approval' ? step.request.definition_path : '';
    const mountedHash = useRendererPreviewHash(cellId, approvalPath);

    // Each step takes the keyboard after the panel focuses the step's body:
    // a name field (the copy's whole path selected, a new name's stem only,
    // so typing keeps the extension), else the step's list, Info's body or
    // the approval; the options list again once a confirm strip goes.
    const duplicating = step?.kind === 'duplicate' ? step.path : null;
    const renaming = step?.kind === 'rename' ? step.path : null;
    useEffect(() => {
        const input = nameInput.current;
        if (!input || (!duplicating && !renaming)) return;
        input.focus({ preventScroll: true });
        if (renaming) input.setSelectionRange(0, splitSuffix(input.value)[0].length);
        else input.select();
    }, [duplicating, renaming]);
    const stepKey = step ? `${step.kind}:${step.path}` : null;
    useEffect(() => {
        if (stepKey && !confirming) stepFocus.current?.focus({ preventScroll: true });
    }, [stepKey, confirming]);
    useEffect(() => {
        stepFocus.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: 'nearest' });
    }, [optionIndex, stepKey]);

    useEffect(() => {
        let live = true;
        fetchHandlers()
            .then(value => { if (live) setHandlers(value); })
            .catch(e => { if (live) setHandlersError(e instanceof Error ? e.message : String(e)); });
        return () => { live = false; };
    }, [channel, cellId]);

    // A query typed and not yet written lands in the session when the
    // panel goes (a no-op once the session closed).
    useEffect(() => {
        const timer = queryTimer;
        const pending = pendingQuery;
        const epoch = generation;
        return () => {
            epoch.current++;
            if (timer.current) clearTimeout(timer.current);
            if (pending.current !== null) useCollimateStore.getState().updatePickSession(cellId, 'files', { query: pending.current });
        };
    }, [cellId]);

    // ── The field: Search's query, Browse's filter or a new entry's name ──
    const writeQuery = () => {
        queryTimer.current = null;
        if (pendingQuery.current === null) return;
        useCollimateStore.getState().updatePickSession(cellId, 'files', { query: pendingQuery.current });
        pendingQuery.current = null;
    };
    /** The session's query becomes `next` now, replacing a pending write. */
    const writeQueryNow = (next: string) => {
        if (queryTimer.current) clearTimeout(queryTimer.current);
        queryTimer.current = null;
        pendingQuery.current = null;
        useCollimateStore.getState().updatePickSession(cellId, 'files', { query: next });
    };
    const onQuery = (next: string) => {
        setError('');
        if (creating) {
            setNewName(next);
            setCompletionIndex(0); setCompletionAccepted(false);
            return;
        }
        setQuery(next);
        // A new search selects its best match.
        setSelectedId(null);
        // Only Search's query is the session's; Browse's filter is the panel's own.
        if (browsing) return;
        pendingQuery.current = next;
        if (queryTimer.current) clearTimeout(queryTimer.current);
        queryTimer.current = setTimeout(writeQuery, QUERY_WRITE_MS);
    };
    const onMode = (next: Mode) => {
        if (next === mode) return;
        setMode(next); setSelectedId(null); setCreating(null); setNewName(''); setError(''); setNotice('');
        if (next === 'browse') {
            // Browse starts at the root with nothing filtered; the search it
            // replaces is gone from the session too.
            setFolder(''); setQuery(''); writeQueryNow('');
        } else {
            // Browse's filter carries over as the search.
            writeQueryNow(query);
        }
    };

    // ── Browse: folders and creation ──────────────────────────────────
    // What just happened stays said until the list moves elsewhere.
    const enter = (path: string) => { setFolder(path); setQuery(''); setSelectedId(null); setNotice(''); };
    /** Up a folder, on the folder just left. */
    const up = () => { setFolder(parentFolder(folder)); setQuery(''); setSelectedId(folder); setNotice(''); };
    // No new entry on a built-in card or inside a run's folder.
    const createRefusal = builtin ? BUILTIN_REFUSAL : runPathRefusal(folder) ?? undefined;
    const startCreating = (kind: Creating) => {
        setCreating(kind); setNewName(''); setQuery('');
        setCompletionIndex(0); setCompletionAccepted(false);
        setError(''); setNotice('');
    };
    const stopCreating = () => { setCreating(null); setNewName(''); setError(''); };
    const completions = creating === 'file' && !busy && !completionAccepted ? filenameCompletions(newName, extensions) : [];
    const completion = completions.length ? completions[completionIndex % completions.length] : undefined;
    const typedName = newName.trim();

    /** Run `action`; its failure is the panel's error (the step's while one
     *  is up). */
    const work = async (action: (live: () => boolean) => Promise<void>) => {
        const token = generation.current;
        const live = () => generation.current === token;
        setBusy(true); setError('');
        try { await action(live); }
        catch (e) { if (live()) setError(e instanceof Error ? e.message : String(e)); }
        finally { if (live()) setBusy(false); }
    };
    const create = () => {
        const kind = creating;
        if (!kind || !typedName || busy) return;
        void work(async live => {
            // One name, in the browsed folder: a path would land elsewhere.
            if (typedName.includes('/') || typedName.includes('\\')) throw new Error('A name can’t contain /');
            const result = kind === 'file' ? await handleCreateFileAtPath(typedName, folder) : await handleCreateFolder(folder, typedName);
            if (!result.ok) throw new Error(result.error);
            if (!live()) return;
            stopCreating();
            setSelectedId(result.path);
            setNotice(`Created ‘${typedName}’.`);
        });
    };
    /** The field's keys while it names a new entry: ↑↓ cycle the extension
     *  completions, Tab accepts one (and never cycles the modes), ↵ creates,
     *  Esc goes back to the folder. */
    const onNameKey = (e: ReactKeyboardEvent<HTMLInputElement>): boolean => {
        if (e.nativeEvent.isComposing || e.altKey || e.ctrlKey || e.metaKey) return false;
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            if (completions.length) setCompletionIndex(at => (at + (e.key === 'ArrowDown' ? 1 : completions.length - 1)) % completions.length);
            return true;
        }
        if (e.key === 'Tab') {
            if (completion && !e.shiftKey) {
                setNewName(completion);
                setCompletionIndex(0); setCompletionAccepted(true); setError('');
            }
            return true;
        }
        if (e.key === 'Enter') { create(); return true; }
        if (e.key === 'Escape') { stopCreating(); return true; }
        return false;
    };
    /** Esc with nothing else to cancel: a new entry's name, else up a
     *  folder; at the root (or in Search) the panel closes. */
    const onEscape = () => {
        if (creating) { stopCreating(); return true; }
        if (browsing && folder) { up(); return true; }
        return false;
    };

    // ── Steps ──────────────────────────────────────────────────────────
    const close = () => closeSession(cellId, 'files');
    const openStep = (next: Step | null, select = 0) => {
        generation.current++;
        setBusy(false); setError(''); setReplace(false); setConfirming(null);
        setOptionIndex(select); setSize(null); setJournal(null); setProvenance(null);
        if (next) setNotice('');
        setStep(next);
    };
    /** Back to the list, saying what happened, on `select` when given. */
    const finish = (message: string, select?: string | null, reveal?: () => void) => {
        openStep(null);
        setNotice(message, reveal);
        if (select !== undefined) setSelectedId(select);
    };
    /** The file's options, on the entry that opens the step `from` (the top
     *  one without it), so ↵ after Back runs that step again. */
    const toOptions = (path: string, from?: Step['kind']) =>
        openStep({ kind: 'options', path }, Math.max(0, fileOptions(path).findIndex(option => option.action === from)));

    const open = (path: string) => {
        close();
        onOpenFile(path);
        focusCellCard(cellId);
    };
    const openWith = (path: string, rendererId: string | undefined) => {
        close();
        onOpenFileWith(path, rendererId);
        focusCellCard(cellId);
    };
    /** Info's facts beyond the file's metadata load as it opens: its size,
     *  the cell's journal for the changes naming it, and its provenance. */
    const openInfo = (path: string) => {
        openStep({ kind: 'info', path });
        const token = generation.current;
        const live = () => generation.current === token;
        const file = cell.files.find(item => item.name === path);
        if (file) fileSizeOf(channel, cellId, file).then(bytes => { if (live()) setSize(bytes); }, () => { if (live()) setSize('unavailable'); });
        else setSize('unavailable');
        fetchJournal(channel, cellId).then(
            events => { if (live()) setJournal(events); },
            e => { if (live()) setError(`The changes could not be loaded: ${e instanceof Error ? e.message : String(e)}`); },
        );
        fetchCellProvenance(channel, cellId).then(
            loaded => { if (live()) setProvenance(loaded); },
            e => { if (live()) setProvenance(`The provenance could not be loaded: ${e instanceof Error ? e.message : String(e)}`); },
        );
    };
    /** A handler's card lives in the Capabilities manage view: close this
     *  panel and reveal the card there, on `step` when given. */
    const showHandler = (cellOf: string, folderOf: string, handlerStep?: 'review') => {
        close();
        revealHandler(cellOf, folderOf, handlerStep);
    };

    // ── Add to Capabilities: the package preview, then the approval ────
    const addTo = (path: string) => work(async live => {
        // The preview reads the stored package: what the editors hold goes first.
        await flushCellSaves(cellId); await flushFileDocuments();
        let request: ApprovalRequest = { source_cell_id: cellId, definition_path: path, target_cell_id: null };
        let preview = await previewApproval(channel, request);
        // The one cell already holding this package is the target, replaced.
        const replacing = preview.targets.filter(target => target.replaces && target.cell_id);
        if (replacing.length === 1) {
            request = { ...request, target_cell_id: replacing[0].cell_id };
            preview = await previewApproval(channel, request);
        }
        if (!live()) return;
        openStep({ kind: 'approval', path, request, preview });
        setReplace(!!request.target_cell_id && !!preview.replacement);
    });
    const chooseTarget = (targetCellId: string | null) => {
        if (step?.kind !== 'approval') return;
        const request = { ...step.request, target_cell_id: targetCellId };
        void work(async live => {
            const preview = await previewApproval(channel, request);
            if (!live()) return;
            setReplace(false);
            setStep({ ...step, request, preview });
        });
    };
    const approve = () => {
        if (step?.kind !== 'approval') return;
        const { request, preview } = step;
        void work(async live => {
            const result = await addApprovedPackage(channel, {
                ...request, expected_content_hash: preview.content_hash, expected_target_revision: preview.target_revision, replace,
            });
            useCollimateStore.getState().applyServerState(await fetchState());
            if (!live()) return;
            // A handler's card waits in Capabilities: on its build's review
            // when the package carried one.
            const folderAdded = typeof preview.replacement?.folder === 'string' ? preview.replacement.folder : preview.folder;
            const reveal = preview.kind === 'handler'
                ? () => showHandler(result.cell_id, folderAdded, preview.summary.derived_hash ? 'review' : undefined)
                : undefined;
            finish(result.message, undefined, reveal);
        });
    };
    /** Offer the package's regenerated skills to the built-in handler it
     *  names; they then wait for review on the built-in's card. */
    const offerSkills = () => {
        if (step?.kind !== 'skills') return;
        const { path, handlerId } = step;
        void work(async live => {
            await flushCellSaves(cellId); await flushFileDocuments();
            const handler = await offerBuiltinSkills(channel, handlerId, { source_cell_id: cellId, folder: parentFolder(path) });
            useCollimateStore.getState().applyServerState(await fetchState());
            if (live()) finish('Skills offered. Review and accept them from the built-in card in Capabilities.', undefined,
                () => showHandler(handler.cell_id, handler.folder, 'review'));
        });
    };

    // ── Duplicate, Rename and Delete ───────────────────────────────────
    const startDuplicate = (path: string) => {
        setStepName(copyPath(path, new Set(cell.files.map(file => file.name))));
        openStep({ kind: 'duplicate', path });
    };
    const duplicate = (path: string, name: string) => work(async live => {
        const destination = name.trim();
        const refusal = duplicateRefusal(destination, new Set(cell.files.map(item => item.name)));
        if (refusal) throw new Error(refusal);
        const file = cell.files.find(item => item.name === path);
        if (!file || lockOwnerOf(path)) throw new Error('The file changed. Retry.');
        const load = withFetchedBodies([file], await fetchTextBodies(channel, cellId, [file]));
        await flushCellSaves(cellId);
        if (!live()) return;
        await onUpdateFiles(files => {
            const original = files.find(item => item.name === path);
            if (!original || files.some(item => item.name === destination) || lockOwnerOf(path)) throw new Error('The file or its copy changed. Retry.');
            const loaded = load(original);
            if (!loaded) throw new Error('The file changed while it was loading. Retry.');
            return replaceFiles(files, [{ ...loaded, name: destination }]);
        });
        if (live()) finish(`Duplicated as ‘${basename(destination)}’.`, destination);
    });
    const startRename = (path: string) => {
        setStepName(basename(path));
        openStep({ kind: 'rename', path });
    };
    /** Through the file operations' rename, so open tabs and documents
     *  follow the file to its new path. */
    const rename = (path: string, raw: string) => work(async live => {
        const name = raw.trim();
        // The same name renames nothing: back to the file's options.
        if (name === basename(path)) { toOptions(path, 'rename'); return; }
        const refusal = renameRefusal(name, path, cell.files, lockOwnerOf);
        if (refusal) throw new Error(refusal);
        wrote.current = false;
        await handleRename({ name: basename(path), path, type: 'file', children: {} }, name);
        if (wrote.current && live()) finish(`Renamed to ‘${name}’.`, within(parentFolder(path), name));
    });
    /** Delete, confirmed in the options step; the list comes back on the
     *  file's neighbour in it, next else previous. */
    const remove = (path: string) => work(async live => {
        const refusal = changeRefusal(path, lockOwnerOf, builtin);
        if (refusal) { setConfirming(null); throw new Error(refusal); }
        const listed = browsing ? entries.map(entry => entry.path) : files.map(file => file.name);
        const at = listed.indexOf(path);
        await onUpdateFiles(current => {
            const remaining = current.filter(file => file.name !== path);
            return remaining.length === current.length ? current : remintEmptiedParent(remaining, path);
        });
        if (live()) finish(`Deleted ${quoted(path)}.`, listed[at + 1] ?? listed[at - 1] ?? null);
    });

    /** Run an entry of the file's options (a refused one does nothing). */
    const runOption = (path: string, option: OptionEntry) => {
        if (option.refusal) return;
        setConfirming(null);
        switch (option.action) {
            case 'approval': if (!busy) void addTo(path); break;
            case 'skills': if (option.skills) openStep(option.skills); break;
            case 'open': open(path); break;
            case 'open-with': openStep({ kind: 'open-with', path }); break;
            case 'info': openInfo(path); break;
            case 'duplicate': startDuplicate(path); break;
            case 'rename': startRename(path); break;
            case 'delete': setConfirming(path); break;
        }
    };

    // ── The list ───────────────────────────────────────────────────────
    const fileRow = (file: VirtualFile): CellPanelRow => ({
        id: file.name,
        name: basename(file.name),
        description: `${file.name} · ${definitionKind(file.name) ?? file.language ?? 'file'}`,
        tags: definitionTags(definitionBodies?.find(body => body.name === file.name) ?? file),
    });
    const creationRow = (id: string, name: string): CellPanelRow => ({ id, name, disabled: !!createRefusal, reason: createRefusal });
    const rows: CellPanelRow[] | undefined = creating ? undefined : browsing ? [
        ...entries.map(entry => (entry.file ? fileRow(entry.file) : { id: entry.path, name: entry.name, description: 'folder' })),
        creationRow(NEW_FILE, 'New file here'),
        creationRow(NEW_FOLDER, 'New folder here'),
    ] : files.map(fileRow);
    const selected = rows?.find(row => row.id === selectedId) ?? rows?.[0];
    const selectedFolder = browsing && !!selected && entries.some(entry => entry.path === selected.id && entry.kind === 'folder');

    const primary: CellPanelPrimary = creating ? {
        label: typedName ? `Create ‘${typedName}’` : 'Create', icon: creating === 'file' ? FilePlus : FolderPlus, intent: 'edit',
        disabled: busy || !typedName, reason: `Name the new ${creating}`, onRun: create,
    } : !selected ? {
        label: 'Options', icon: Ellipsis, intent: 'edit', disabled: true, reason: 'No file selected', onRun: () => {},
    } : selected.id === NEW_FILE || selected.id === NEW_FOLDER ? {
        label: selected.name, icon: selected.id === NEW_FILE ? FilePlus : FolderPlus, intent: 'edit',
        disabled: busy || !!createRefusal, reason: createRefusal, onRun: () => startCreating(selected.id === NEW_FILE ? 'file' : 'folder'),
    } : selectedFolder ? {
        label: `Open ${quoted(selected.id)}`, icon: Folder, intent: 'run', disabled: busy, onRun: () => enter(selected.id),
    } : {
        label: `Options for ${quoted(selected.id)}`, icon: Ellipsis, intent: 'edit', disabled: busy, onRun: () => toOptions(selected.id),
    };
    const toggle = (key: keyof FilesPanelFilters) => () => {
        onLayoutChange(l => setFilesPanelFilters(l, { [key]: !filters[key] }));
        setSelectedId(null);
    };

    // ── The steps ──────────────────────────────────────────────────────
    /** The renderer the file's open tab shows (its Open with override, else
     *  the default), or null while no tab shows the file. */
    const tabRenderer = (path: string) => {
        const tab = findFileTab(layout, path)?.tab;
        return tab?.kind === 'file' ? resolveRendererWithOverride(path, tab.rendererId) : null;
    };

    /** The Capabilities entry of a file's options: Add to Capabilities for a
     *  definition package in a cell off Capabilities (never a built-in
     *  card's); for a handler with a built-in handler's id, Use these skills
     *  instead. */
    const capabilityOption = (path: string): OptionEntry | null => {
        const kind = definitionKind(path);
        if (builtin || onCapabilities || !isCapabilityKind(kind)) return null;
        const pkg = definitionBodies?.find(body => body.name === path) ?? cell.files.find(item => item.name === path);
        if (!pkg) return null;
        const name = packageName(pkg);
        const id = definitionMeta(pkg)?.id;
        if (kind === 'handler' && typeof id === 'string' && builtinHandlerIds.has(id)) {
            const folderOf = parentFolder(path);
            return {
                id: 'skills', action: 'skills', icon: Sparkles,
                label: `Use these skills for the built-in ‘${name}’ handler…`,
                refusal: regeneratedSkills(cell.files, folderOf).length ? undefined : `No regenerated skills under ${within(folderOf, 'derived/skills')}`,
                skills: { kind: 'skills', path, handlerId: id, name },
            };
        }
        return { id: 'approval', action: 'approval', icon: PackagePlus, label: `Add ‘${name}’ to Capabilities…` };
    };
    /** A file's options, in order. */
    const fileOptions = (path: string): OptionEntry[] => {
        const capability = capabilityOption(path);
        const changeReason = changeRefusal(path, lockOwnerOf, builtin);
        return [
            ...(capability ? [capability] : []),
            // Open shows the file as its open tab does (an Open with override stays).
            { id: 'open', action: 'open', label: 'Open', description: `with ${rendererLabel(tabRenderer(path) ?? resolveRenderer(path))}` },
            { id: 'open-with', action: 'open-with', label: 'Open with…', description: 'Choose any renderer' },
            { id: 'info', action: 'info', label: 'Info' },
            { id: 'duplicate', action: 'duplicate', label: 'Duplicate…', refusal: changeReason },
            // A run's files keep the names its run gave them.
            { id: 'rename', action: 'rename', label: 'Rename…', refusal: changeReason ?? (isRunPath(path) ? 'A run’s files keep their names' : undefined) },
            { id: 'delete', action: 'delete', label: 'Delete…', refusal: changeReason },
        ];
    };

    let stepNode: ReactNode = null;
    if (step?.kind === 'options') {
        const { path } = step;
        const title = quoted(path);
        stepNode = <StepFrame title={title} error={error}>
            <StepList ref={stepFocus} cellId={cellId} title={title} entries={fileOptions(path)} index={optionIndex} onIndex={setOptionIndex} onRun={option => runOption(path, option)} />
        </StepFrame>;
    }
    if (step?.kind === 'open-with') {
        const { path } = step;
        // The renderers that claim the file, in the right-click's order, then
        // every other one in the registry: any of them may open it.
        const candidates = rendererCandidatesFor(path);
        const defaultId = resolveRenderer(path).id;
        const currentId = tabRenderer(path)?.id;
        const entry = (renderer: ResolvedRenderer, section: RendererEntry['section']): RendererEntry => ({
            id: renderer.id, section, label: rendererLabel(renderer),
            description: [renderer.id === defaultId && 'default', renderer.id === currentId && 'current'].filter(Boolean).join(' · ') || undefined,
            // The default clears a tab's override, as the right-click's Open with does.
            rendererId: renderer.id === defaultId ? undefined : renderer.id,
        });
        const renderers = [
            ...candidates.map(renderer => entry(renderer, 'For this file')),
            ...allRenderers().filter(renderer => !candidates.some(claimed => claimed.id === renderer.id)).map(renderer => entry(renderer, 'Other renderers')),
        ];
        const title = `Open ${quoted(path)} with`;
        stepNode = <StepFrame title={title} error={error}>
            <StepList ref={stepFocus} cellId={cellId} title={title} entries={renderers} index={optionIndex} onIndex={setOptionIndex} onRun={renderer => openWith(path, renderer.rendererId)} />
        </StepFrame>;
    }
    if (step?.kind === 'info') {
        const { path } = step;
        const file = cell.files.find(item => item.name === path);
        // The type rule the file falls under: its handler, or none because
        // the type is claimed by more than one (a clash), or no rule at all.
        const rule = handlers && handlerTypeForPath(handlerTypeRules(handlers), path);
        const handler = rule?.handler ?? null;
        const ext = suffix(path);
        const where = parentFolder(path);
        const changes = journal ? fileEvents(journal, path) : [];
        const edited = journal && lastChange(journal, path);
        const created = journal && firstChange(journal, path);
        const facts: (readonly [string, ReactNode])[] = [
            ['Path', <span className={MONO}>{path}</span>],
            ['Folder', where ? <span className={MONO}>{where}</span> : ROOT_LABEL],
            ['Size', size === null ? 'Loading…' : size === 'unavailable' ? 'Unavailable' : formatBytes(size)],
            ['Type', definitionKind(path) ?? (file?.encoding === 'binary' ? 'binary' : file?.language || 'file')],
            // The journal's error is the step's.
            ['Last edited', !journal ? (error ? 'Unavailable' : 'Loading…') : edited ? fileTime(edited, 'On or after') : 'Unknown'],
            ...(created ? [['Created', fileTime(created, 'On or before')] as const] : []),
        ];
        const loaded = typeof provenance === 'object' ? provenance : null;
        const record = loaded?.records.find(item => path.startsWith(`${item.folder}/`));
        const provenanceFacts: (readonly [string, ReactNode])[] = record ? [
            ['Source', <>{record.source_kind}{record.source_id !== record.source_kind && <> · <span className={MONO}>{record.source_id}</span></>}</>],
            ['Package', `${record.package_id} (${record.package_kind})`],
            ['Version', record.package_version ?? 'None'],
            ['Content hash', <span className={MONO} title={record.content_hash}>{record.content_hash.slice(0, 12)}</span>],
            ['SDK', record.sdk_version],
        ] : [];
        const handlerNote = handlersError ? <p role="alert" className={cn(STEP_NOTE, 'text-red')}>The handlers could not be loaded: {handlersError}</p>
            : !handlers ? <p className={STEP_NOTE}>Loading handlers…</p>
            : handler ? null
            : rule ? <p className={STEP_NOTE}>More than one handler claims ‘{rule.ext}’ files, so none is in force.</p>
            : <p className={STEP_NOTE}>{ext ? `No handler claims ‘${ext}’ files.` : 'No handler claims this file.'}</p>;
        const title = `Info on ${quoted(path)}`;
        stepNode = <StepFrame title={title} error={error}>
            {/* ↵ on the step itself (not on the handler's button) goes back
                to the file's options, as Esc does. */}
            <div ref={stepFocus} role="group" aria-label={title} tabIndex={-1} className="flex flex-col gap-3 outline-none"
                onKeyDown={e => {
                    if (e.key !== 'Enter' || e.target !== e.currentTarget) return;
                    e.preventDefault();
                    toOptions(path, 'info');
                }}>
                <Section label="File"><Facts rows={facts} /></Section>
                <Section label="Handler">{handlerNote ?? (handler && <button type="button" className={STEP_ROW} title="Show the handler’s card in Capabilities" onClick={() => showHandler(handler.cell_id, handler.folder)}>
                    <span className="flex items-center gap-2">
                        <span className="font-medium text-ink">{handler.name}</span>
                        {handler.builtin && <span className={chip}>built-in</span>}
                    </span>
                    {handler.description && <span className="mt-px block text-[0.75rem] leading-[1.4] text-ink-3">{handler.description}</span>}
                </button>)}</Section>
                {typeof provenance === 'string' && <Section label="Provenance"><p role="alert" className={cn(STEP_NOTE, 'text-red')}>{provenance}</p></Section>}
                {record && loaded && <Section label="Provenance">
                    <Facts rows={provenanceFacts} />
                    {record.sdk_version !== loaded.sdk_version && <p className={STEP_NOTE}>Admitted under SDK {record.sdk_version}; this app runs SDK {loaded.sdk_version}.</p>}
                </Section>}
                {/* A journal that failed to load is the step's error, never "no change". */}
                {(journal || !error) && <Section label="Recent changes">
                    {!journal ? <p className={STEP_NOTE}>Loading changes…</p>
                        : changes.length === 0 ? <p className={STEP_NOTE}>No recorded change names this file.</p>
                        : <ul className="flex flex-col gap-1 px-2 text-[0.78125rem] text-ink-2">{changes.slice(0, RECENT_CHANGES).map((event, at) => <li key={`${event.t}:${at}`}>
                            {eventLabel(event)} · {when(event.t)}
                            {event.run?.note && <span className="block text-[0.71875rem] italic text-ink-3">{event.run.note}</span>}
                        </li>)}</ul>}
                </Section>}
            </div>
        </StepFrame>;
    }
    if (step?.kind === 'approval') {
        const { preview, request } = step;
        const { derived_hash: derivedHash, kept_from_target: kept, ...summary } = preview.summary;
        const summaryName = typeof summary.name === 'string' ? summary.name : preview.id;
        const differs = !!mountedHash && mountedHash !== preview.content_hash;
        stepNode = <StepFrame title={`Add ‘${summaryName}’ to Capabilities`} error={error}
            footer={<button type="button" className={btnPrimary} disabled={busy || (!!preview.replacement && !replace)} onClick={approve}>
                <PackagePlus className="h-3.5 w-3.5" />Confirm
            </button>}>
            <div ref={stepFocus} tabIndex={-1} className="flex flex-col gap-3 outline-none">
                <Section label="Target">
                    <div className="flex flex-col">
                        {preview.targets.map(target => {
                            const on = target.cell_id === request.target_cell_id;
                            return <button key={target.cell_id ?? 'new'} type="button" aria-pressed={on} disabled={busy}
                                className={cn(STEP_ROW, 'flex items-center gap-2', on && 'bg-sheet-2')} onClick={() => chooseTarget(target.cell_id)}>
                                <span className={cn('font-medium', on ? 'text-ink' : 'text-ink-2')}>{target.name}</span>
                            </button>;
                        })}
                    </div>
                </Section>
                <Section label="Package">
                    <Facts rows={[
                        ['Id', <span className={MONO}>{preview.id}</span>],
                        ['Kind', preview.kind],
                        ['Content hash', <span className={MONO}>{preview.content_hash}</span>],
                        ['Last mounted preview', mountedHash ? <span className={MONO}>{mountedHash}</span> : 'No mounted preview recorded'],
                    ]} />
                    {differs && <p className="text-[0.75rem] text-ink-2">The mounted preview differs from the current package.</p>}
                    {derivedHash && <p className="text-[0.75rem] text-ink-2">Build <span className={MONO}>{derivedHash}</span>: waits for your review</p>}
                    {kept.length > 0 && <div className="text-[0.75rem] text-ink-2">Kept from the cell:
                        <ul className="mt-0.5 flex flex-col gap-0.5">{kept.map(path => <li key={path} className={MONO}>{path}</li>)}</ul>
                    </div>}
                </Section>
                {Object.keys(summary).length > 0 && <Section label="Summary">
                    <Facts rows={Object.entries(summary).map(([key, value]) => [key, plain(value)] as const)} />
                </Section>}
                <Section label="Files">
                    <ul className="flex flex-col gap-0.5">{preview.files.map(path => <li key={path} className={MONO}>{path}</li>)}</ul>
                </Section>
                {preview.replacement && <Section label="Replaces">
                    <Facts rows={Object.entries(preview.replacement).map(([key, value]) => [key, plain(value)] as const)} />
                    <label className="mt-1 flex items-center gap-2 text-[0.78125rem] text-ink-2">
                        <Switch checked={replace} onCheckedChange={setReplace} aria-label="Replace" />
                        Replace the existing package
                    </label>
                </Section>}
            </div>
        </StepFrame>;
    }
    if (step?.kind === 'skills') {
        const { path, name } = step;
        const skills = regeneratedSkills(cell.files, parentFolder(path));
        stepNode = <StepFrame title={`Use these skills for the built-in ‘${name}’ handler`} error={error}
            footer={<button type="button" className={btnPrimary} disabled={busy || !skills.length} onClick={offerSkills}>
                <Sparkles className="h-3.5 w-3.5" />Use skills
            </button>}>
            <div ref={stepFocus} tabIndex={-1} className="flex flex-col gap-3 outline-none">
                <p className="text-[0.78125rem] text-ink-2">The built-in handler’s checks never change. Its skills the app pins stay as they are; the others are replaced by these and wait for your review on its card in Capabilities.</p>
                <Section label="Skills">
                    <ul className="flex flex-col gap-0.5">{skills.map(skill => <li key={skill} className={MONO}>{skill}</li>)}</ul>
                </Section>
            </div>
        </StepFrame>;
    }
    if (step?.kind === 'duplicate' || step?.kind === 'rename') {
        const { kind, path } = step;
        const copying = kind === 'duplicate';
        const label = copying ? 'Duplicate name' : 'New name';
        const verb = copying ? 'Duplicate' : 'Rename';
        const Icon = copying ? Copy : Pencil;
        stepNode = <form className="contents" onSubmit={e => { e.preventDefault(); void (copying ? duplicate(path, stepName) : rename(path, stepName)); }}>
            <StepFrame title={`${verb} ${quoted(path)}`} error={error}
                footer={<button type="submit" className={btnPrimary} disabled={busy || !stepName.trim()}><Icon className="h-3.5 w-3.5" />{verb}</button>}>
                <label className="flex flex-col gap-1">
                    <span className={formLabel}>{label}</span>
                    <input ref={nameInput} className={cn(formInput, 'font-mono')} aria-label={label} spellCheck={false} autoComplete="off"
                        value={stepName} onChange={e => { setStepName(e.target.value); setError(''); }} />
                </label>
            </StepFrame>
        </form>;
    }

    return <CellPanel<Mode>
        cellId={cellId}
        kind="files"
        host={host}
        title="Files"
        searchActions={browsing ? undefined : [
            { id: 'runs', label: 'Runs', icon: Activity, pressed: filters.runsOnly, onRun: toggle('runsOnly') },
            { id: 'recent', label: 'Recent', icon: Clock, pressed: filters.sortByRecency, onRun: toggle('sortByRecency') },
        ]}
        note={notice ? <span role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span>{notice.text}</span>
            {notice.reveal && <button type="button" className={btn} onClick={notice.reveal}>Show in Capabilities</button>}
        </span> : undefined}
        // While the field names a new entry, Tab accepts its completion: the
        // other mode waits until the entry is made or let go.
        modes={creating ? MODES.map(m => (m.id === mode ? m : { ...m, disabled: true, reason: `Create or cancel the new ${creating} first` })) : MODES}
        mode={mode}
        onMode={onMode}
        location={browsing ? <>
            <Folder className="h-3.5 w-3.5 flex-none" aria-hidden />
            {/* A deep folder gives up its start, keeping the folder you are in. */}
            <span className="min-w-0 truncate font-mono [direction:rtl]"><bdi>{folder || ROOT_LABEL}</bdi></span>
        </> : undefined}
        query={creating ? newName : query}
        onQuery={onQuery}
        placeholder={creating ? `Name the new ${creating}` : browsing ? 'Filter this folder' : 'Find file'}
        searchIcon={creating === 'file' ? FilePlus : creating === 'folder' ? FolderPlus : undefined}
        completion={completion?.slice(newName.length)}
        fieldNote={creating === 'file' ? <>
            Default renderer: <span className="text-ink">{newRenderer?.name ?? (newRendererError ? 'Unavailable' : 'Loading…')}</span>
            {completion && ' · Tab to accept · ↑↓ to change extension'}
        </> : undefined}
        onFieldKey={creating ? onNameKey : undefined}
        onEscape={onEscape}
        error={error || handlersError || undefined}
        rows={rows}
        selectedId={selected?.id ?? null}
        onSelect={setSelectedId}
        clickRuns
        empty={query ? 'No file matches' : filters.runsOnly ? 'No file is held by a live run' : 'This cell has no files'}
        primary={primary}
        hints={[{ keys: 'esc', label: creating ? 'cancel' : browsing && folder ? 'up a folder' : 'close' }]}
        confirm={confirming ? {
            message: `Delete ${quoted(confirming)}?`, confirmLabel: 'Delete', busy,
            onConfirm: () => remove(confirming), onCancel: () => setConfirming(null),
        } : undefined}
        // Every step but the options goes back to them, on its entry; they go back to the list.
        step={step ? { body: stepNode, onBack: () => (step.kind === 'options' ? openStep(null) : toOptions(step.path, step.kind)) } : undefined}
        onClose={close}
    />;
}
