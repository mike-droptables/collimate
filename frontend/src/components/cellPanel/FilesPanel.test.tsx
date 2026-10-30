import { fileFixture, fixtureText } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useEffect, useRef, useState } from 'react';
import { routeApiToFetchStub } from '@/test/apiTransport';
import { FilesPanel } from './FilesPanel';
import type { CellPanelHost } from './CellPanel';
import { FilesPane } from '@/components/cellEditor/FilesPane';
import { createDefaultLayout, openFileTab, setFilesPane } from '@/lib/cellLayout';
import { getSupportedExtensions, refreshRendererCache, rendererCandidatesFor, resolveRenderer } from '@/hooks/useRendererId';
import { executeFindFile } from '@/hooks/sceneKeyboardEffects';
import { buildForwardedCombos } from '@/lib/keybindings/forwardedCombos';
import { is } from '@/lib/keybindings/handlers';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { KEYBOARD_OWNER_ATTR } from '@/lib/keyboardOwner';
import { filenameCompletions } from '@/lib/filenameCompletions';
import { eventLabel, formatBytes } from '@/lib/fileInfo';
import { when } from '@/components/overview/journalText';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, Channel, RunRecord, VirtualFile } from '@/lib/types';
import type { CellLayout } from '@/stores/collimate/types';
import type { ApprovalPreview, HandlerInfo } from '@/lib/definitionApi';
import type { CellProvenance, JournalEvent, JournalRun, ProvenanceRecord } from '@/lib/api/cells';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchState: () => Promise.resolve({ active_channel_id: 'channel', channel_scope: 'channel', channels: [channel] }),
}));

const vf = (name: string, content = '', timestamp = '2026-01-01', extra: Partial<VirtualFile> = {}): VirtualFile =>
    (fileFixture(name, content, {timestamp, language: name.endsWith('.renderer') ? 'yaml' : 'text', ...extra}));
const RUN: RunRecord = {
    runId: 'run', cellId: 'cell', channel: 'test', runnerId: 'r', runnerName: 'Builder', generatorName: 'Generator',
    label: 'r-00000000', folder: '_runs/r-00000000',
} as RunRecord;
/** Paths a test locks on top of the run folder's, while its run is live. */
const held = new Set<string>();
let runLive = true;
const lock = (path: string) => ((runLive && path.startsWith('_runs/')) || held.has(path) ? RUN : null);
const FILES = [
    vf('notes.txt', 'notes', '2026-01-02'),
    vf('pkg/view.renderer', 'id: view\nname: View\nentrypoint: main.tsx'),
    vf('_runs/r-00000000/log.txt', 'log', '2026-10-03'),
    vf('selected.md', 'selected', '2026-01-01'),
    vf('docs/.collkeep'),
];
/** The root as Browse/Create lists it: folders, files, then the creation rows. */
const ROOT = ['_runs', 'docs', 'pkg', 'notes.txt', 'selected.md', 'New file here', 'New folder here'];

const spies = { open: vi.fn(), openWith: vi.fn(), update: vi.fn() };
let layoutNow: CellLayout;
let filesNow: VirtualFile[];

function Harness({ initial = FILES, active = 'selected.md', builtin = false, host, tabs = l => l }: {
    initial?: VirtualFile[]; active?: string | null; builtin?: boolean; host?: CellPanelHost;
    /** The cell's open tabs, on the default layout. */
    tabs?: (layout: CellLayout) => CellLayout;
}) {
    const [layout, setLayout] = useState(() => tabs(setFilesPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }),
        { collapsed: false, selection: { nodePath: 'pkg/view.renderer', isFolder: false } })));
    const [files, setFiles] = useState(initial);
    const latest = useRef(initial);
    useEffect(() => { layoutNow = layout; filesNow = files; });
    const session = useCollimateStore(s => s.pickSessions.cell);
    if (session?.kind !== 'files') return null;
    const cell: Cell = { id: 'cell', name: 'Work', files, builtin };
    return <FilesPanel cell={cell} channel="test" layout={layout} onLayoutChange={fn => setLayout(fn)} activePath={active}
        lockOwnerOf={lock} host={host}
        onUpdateFiles={(fn, options) => { spies.update(fn, options); const next = fn(latest.current); latest.current = next; setFiles(next); }}
        onOpenFile={spies.open} onOpenFileWith={spies.openWith} />;
}

const channel: Channel = {
    id: 'channel', name: 'test', nav_stacks: [], nav_selection_id: null, main_selection_id: null,
    main_stacks: [
        { id: 'capabilities', name: 'Capabilities', stack_type: 'capabilities', cells: [] },
        { id: 'work-stack', name: 'Work', stack_type: 'standard', cells: [{ id: 'cell', name: 'Work', files: FILES }] },
    ],
} as Channel;
/** The approval preview /packages/preview serves; a Shipped target carries a replacement. */
let preview: ApprovalPreview;
const PREVIEW: ApprovalPreview = {
    targets: [{ cell_id: null, name: 'New cell', replaces: false }, { cell_id: 'shipped', name: 'Shipped', replaces: false }],
    content_hash: 'a'.repeat(64), source_version_id: 'source-tip', kind: 'renderer', id: 'view', folder: 'pkg',
    files: ['view.renderer', 'main.tsx'], summary: { name: 'View', embed: 'native', extensions: ['.demo'], derived_hash: null, kept_from_target: [] },
    replacement: null, target_revision: 'shown-target',
};
/** The message /packages/add answers with. */
let addMessage = 'Added';
const requests: { url: string; body: Record<string, unknown>; init?: RequestInit }[] = [];
/** The handler catalog /handlers serves (the panel's and the renderer registry's). */
let catalog: HandlerInfo[] = [];
/** The page of the cell's journal /journal serves (newest first). */
let journalPage: JournalEvent[] = [];
/** The cell's provenance /provenance serves. */
let provenance: CellProvenance = { sdk_version: '0.1.0', records: [] };
/** /packages/add refuses with a stale-package 409. */
let refusal = false;
let restoreTransport: () => void;

function serve(extra?: (path: string) => unknown) {
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
        const path = String(url);
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        requests.push({ url: path, body, init });
        const special = extra?.(path);
        if (special !== undefined) return special;
        if (path.endsWith('/handlers')) return { ok: true, json: async () => catalog };
        if (path.includes('/cells/cell/journal?')) return { ok: true, json: async () => journalPage };
        if (path.endsWith('/cells/cell/provenance')) return { ok: true, json: async () => provenance };
        if (path.endsWith('/packages/preview')) return { ok: true, json: async () => (body.target_cell_id === 'shipped' ? { ...preview, replacement: { id: preview.id, folder: 'kept-folder', old_version: 'old' } } : preview) };
        if (path.endsWith('/packages/add')) return { ok: !refusal, status: refusal ? 409 : 200, json: async () => (refusal ? { detail: 'The package changed; inspect again' } : { cell_id: 'new', message: addMessage }) };
        return { ok: true, json: async () => ({}) };
    }));
}
/** Load `renderers` (and the catalog's rendered-as aliases) into the renderer registry; [] empties it.
 *  The built-in text renderer, as in the app, is a default for every text file (`default_for: ['*']`). */
async function seatRenderers(renderers: { id: string; name: string; extensions: string[]; default_for?: string[] }[]) {
    serve(path => (path.endsWith('/renderers') ? { ok: true, json: async () => renderers } : undefined));
    await act(async () => { await refreshRendererCache(); });
}

const journalRun = (extra: Partial<JournalRun> = {}): JournalRun => ({
    run_id: 'run', runner_id: 'r', runner_name: 'Builder', status: 'succeeded', outcome: null, note: null,
    started_at: null, ended_at: null, files_written: [], ...extra,
});
const journalEvent = (t: string, kind: JournalEvent['kind'], extra: Partial<JournalEvent> = {}): JournalEvent => ({
    t, kind, by: { kind: 'user', runner_id: null, runner_name: null }, cells: [], run: null, files: [], files_total: 0, version_id: null, ...extra,
});

const openSession = () => act(() => useCollimateStore.getState().openPickSession('cell', 'files'));
const panel = () => screen.getByRole('dialog', { name: 'Files' });
/** The panel's one field, whatever it means in the mode. */
const field = () => screen.getByRole('combobox');
const search = () => screen.getByRole('combobox', { name: 'Find file' });
const list = () => screen.getByRole('listbox', { name: 'Files' });
const names = () => within(list()).getAllByRole('option').map(o => o.firstElementChild?.textContent);
const option = (name: string) => within(list()).getAllByRole('option').find(o => o.firstElementChild?.textContent === name)!;
/** A row's description line. */
const description = (name: string) => option(name).children[1]?.textContent;
const modeTabs = () => within(screen.getByRole('group', { name: 'Mode' })).getAllByRole('button');
const activeMode = () => modeTabs().find(b => b.getAttribute('aria-pressed') === 'true')?.textContent;
const location = () => screen.getByTestId('cell-panel-location');
const hints = () => document.querySelector('[data-cell-panel-hints]');
const primary = () => screen.getByTestId('cell-panel-primary');
const toBrowse = () => fireEvent.keyDown(field(), { key: 'Tab' });
/** Browse into `folder` (a child of the current one): a click on its row. */
const enterFolder = (folder: string) => fireEvent.click(option(folder));
/** Select the row `name` from the field with ↓ (a click would run it). */
const selectRow = (name: string) => {
    for (let at = 0; at < names().length && option(name).getAttribute('aria-selected') !== 'true'; at++) fireEvent.keyDown(field(), { key: 'ArrowDown' });
};

/** The options step: one listbox named after the file. */
const optionsList = () => screen.getByRole('listbox', { name: /^‘.*’$/ });
const entries = () => within(optionsList()).getAllByRole('option');
const entryLabels = () => entries().map(o => o.firstElementChild?.textContent);
const entry = (label: string) => entries().find(o => o.firstElementChild?.textContent === label)!;
const selectedEntry = () => entries().find(o => o.getAttribute('aria-selected') === 'true')?.firstElementChild?.textContent;
const headings = () => screen.getAllByRole('heading').map(h => h.textContent);
/** The Open with step: one listbox, "Open ‘name’ with". */
const rendererList = () => screen.getByRole('listbox', { name: /^Open ‘.*’ with$/ });
const renderer = (name: string) => within(rendererList()).getAllByRole('option').find(o => o.firstElementChild?.textContent === name)!;
/** A renderer's marks: 'default', 'current', both, or none. */
const rendererNote = (name: string) => renderer(name).children[1]?.textContent;
const selectedRenderer = () => within(rendererList()).getAllByRole('option').find(o => o.getAttribute('aria-selected') === 'true')?.firstElementChild?.textContent;
const sectionNames = (section: string) => within(screen.getByRole('group', { name: section })).getAllByRole('option').map(o => o.firstElementChild?.textContent);
/** A registry where `a.txt` is claimed by Zed notes (its default), Alpha
 *  view and Text, in that order, and two renderers claim other files. */
const RENDERERS = [
    { id: 'zed', name: 'Zed notes', extensions: ['.txt'] },
    { id: 'board', name: 'Wall board', extensions: ['.board'] },
    { id: 'alpha', name: 'Alpha view', extensions: ['.txt'] },
    { id: 'chart', name: 'Bar chart', extensions: ['.chart'] },
    { id: 'text', name: 'Text', extensions: ['.txt'], default_for: ['*'] },
];

const handlerInfo = (id: string, extensions: string[], clashes: string[] = [], disabled = false): HandlerInfo => ({
    id, name: `${id} handler`, description: `What ${id} checks`, types: extensions.map(ext => ({ ext })), examples: [],
    cell_id: `${id}-cell`, folder: id, definition_path: `${id}/${id}.handler`, has_derived: true,
    built_hash: 'hash', accepted: true, accepted_derived: 'hash', disabled, clashes, accepted_skills: [], added_skills: [], builtin: false,
    uses_app_skills: false,
});

async function mount(props: Parameters<typeof Harness>[0] = {}) {
    openSession();
    await act(async () => { render(<Harness {...props} />); });
}
/** Open the options of the file `name`: a click on its row. */
const optionsOf = (name: string) => fireEvent.click(option(name));
/** Mount on `files` and open the first one's Info. */
async function info(files: VirtualFile[]) {
    await mount({ initial: files, active: null });
    fireEvent.keyDown(search(), { key: 'Enter' });
    await act(async () => { fireEvent.click(entry('Info')); });
}
const handlerSection = () => screen.getByRole('heading', { name: 'Handler' }).parentElement!;
/** A fact of Info's File section, by its label. */
const fact = (label: string) => within(screen.getByRole('group', { name: 'File' })).getByText(label).nextElementSibling?.textContent;

beforeEach(() => {
    restoreTransport = routeApiToFetchStub();
    requests.length = 0; catalog = []; journalPage = []; provenance = { sdk_version: '0.1.0', records: [] }; refusal = false; held.clear(); runLive = true; preview = PREVIEW; addMessage = 'Added';
    Object.values(spies).forEach(spy => spy.mockReset());
    useCollimateStore.setState({
        activeChannel: channel, state: { active_channel_id: channel.id, channels: [channel] },
        cellCache: { cell: channel.main_stacks[1].cells[0] }, pickSessions: {}, cellHistory: {}, cellDiffs: {},
        cellLayouts: {}, pendingSelectCellId: null, capabilitiesManageRequest: null,
    });
    serve();
});
afterEach(() => { cleanup(); restoreTransport(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('FilesPanel: Search', () => {
    it('is the cell\'s Files panel: a dialog over the body that owns the keyboard, its search focused, in Search of two modes (P03)', async () => {
        await mount();
        expect(panel()).toHaveAttribute('data-cell-panel', 'files');
        expect(panel()).toHaveAttribute('data-pick-cell', 'cell');
        expect(panel()).toHaveAttribute(KEYBOARD_OWNER_ATTR);
        expect(panel()).not.toHaveAttribute('aria-modal');
        expect(document.activeElement).toBe(search());
        expect(modeTabs().map(b => b.textContent)).toEqual(['Search', 'Browse/Create']);
        expect(activeMode()).toBe('Search');
        expect(screen.queryByTestId('cell-panel-location')).toBeNull();
    });

    it('lists the active tab\'s file, then the tree selection, then path order, each over its full path and kind, a definition with its id and name (P03)', async () => {
        await mount();
        expect(names()).toEqual(['selected.md', 'view.renderer', 'log.txt', 'notes.txt']);
        expect(description('view.renderer')).toBe('pkg/view.renderer · renderer');
        expect(within(option('view.renderer')).getByText('view')).toBeInTheDocument();
        expect(within(option('view.renderer')).getByText('View')).toBeInTheDocument();
        // A top-level file's path shows too.
        expect(description('notes.txt')).toBe('notes.txt · text');
        expect(description('log.txt')).toBe('_runs/r-00000000/log.txt · text');
        expect(option('selected.md')).toHaveAttribute('aria-selected', 'true');
        expect(primary()).toHaveTextContent('Options for ‘selected.md’');
    });

    it('filters by glob or substring; Runs and Recent are pressed toggles beside the search, kept in the layout (P03)', async () => {
        await mount();
        const row = search().closest('label')!.parentElement!;
        for (const label of ['Runs', 'Recent']) expect(within(row).getByRole('button', { name: label })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Close' }).parentElement!.querySelectorAll('button')).toHaveLength(1);
        fireEvent.change(search(), { target: { value: '*.txt' } });
        expect(names()).toEqual(['log.txt', 'notes.txt']);
        fireEvent.change(search(), { target: { value: 'pkg/' } });
        expect(names()).toEqual(['view.renderer']);
        fireEvent.change(search(), { target: { value: '' } });
        const runs = screen.getByRole('button', { name: 'Runs' });
        expect(runs).toHaveAttribute('aria-pressed', 'false');
        fireEvent.mouseDown(runs);
        fireEvent.click(runs);
        expect(runs).toHaveAttribute('aria-pressed', 'true');
        expect(runs).toHaveClass('text-teal');
        expect(names()).toEqual(['log.txt']);
        expect(layoutNow.filesPane.panelFilters).toEqual({ runsOnly: true, sortByRecency: false });
        fireEvent.click(runs);
        fireEvent.click(screen.getByRole('button', { name: 'Recent' }));
        expect(names()).toEqual(['log.txt', 'notes.txt', 'view.renderer', 'selected.md']);
        expect(layoutNow.filesPane.panelFilters).toEqual({ runsOnly: false, sortByRecency: true });
        fireEvent.change(search(), { target: { value: 'nothing-here' } });
        expect(list()).toHaveTextContent('No file matches');
    });

    it('writes the query into the session 250 ms after typing, and on close (P03)', async () => {
        await mount();
        vi.useFakeTimers();
        fireEvent.change(search(), { target: { value: '*.md' } });
        act(() => { vi.advanceTimersByTime(200); });
        expect(useCollimateStore.getState().pickSessions.cell.query).toBe('');
        act(() => { vi.advanceTimersByTime(50); });
        expect(useCollimateStore.getState().pickSessions.cell.query).toBe('*.md');
        vi.useRealTimers();
        // The panel remounts (a zoom, a culled cell) on its session's query.
        cleanup();
        await act(async () => { render(<Harness />); });
        expect(search()).toHaveValue('*.md');
        expect(names()).toEqual(['selected.md']);
        // A query typed just before the panel goes is written as it goes.
        fireEvent.change(search(), { target: { value: 'notes' } });
        cleanup();
        expect(useCollimateStore.getState().pickSessions.cell.query).toBe('notes');
    });

    it('only Search\'s query is the session\'s: Browse clears it and its filter is never written; back in Search the filter is the search (P03)', async () => {
        await mount();
        vi.useFakeTimers();
        fireEvent.change(search(), { target: { value: 'notes' } });
        toBrowse();
        expect(useCollimateStore.getState().pickSessions.cell.query).toBe('');
        fireEvent.change(field(), { target: { value: 'pkg' } });
        act(() => { vi.advanceTimersByTime(1000); });
        expect(useCollimateStore.getState().pickSessions.cell.query).toBe('');
        fireEvent.keyDown(field(), { key: 'Tab' });
        expect(activeMode()).toBe('Search');
        expect(search()).toHaveValue('pkg');
        expect(useCollimateStore.getState().pickSessions.cell.query).toBe('pkg');
        expect(names()).toEqual(['view.renderer']);
    });

    it('renders in the Commands pane\'s slot as in its cell (host pane) (P03)', async () => {
        await mount({ host: 'pane' });
        expect(panel()).toHaveAttribute('data-cell-panel-host', 'pane');
        expect(panel()).not.toHaveAttribute(KEYBOARD_OWNER_ATTR);
        expect(document.querySelector('[data-cell-panel-scrim]')).toBeNull();
        expect(names()).toEqual(['selected.md', 'view.renderer', 'log.txt', 'notes.txt']);
        toBrowse();
        expect(names()).toEqual(ROOT);
    });
});

describe('FilesPanel: Browse/Create', () => {
    it('Tab switches to Browse/Create: the field clears and the root lists its folders, then its files, then New file here and New folder here (P03)', async () => {
        await mount();
        fireEvent.change(search(), { target: { value: 'notes' } });
        toBrowse();
        expect(activeMode()).toBe('Browse/Create');
        expect(field()).toHaveValue('');
        expect(field()).toHaveAccessibleName('Filter this folder');
        expect(location()).toHaveTextContent('root');
        expect(names()).toEqual(ROOT);
        expect(description('docs')).toBe('folder');
        expect(description('notes.txt')).toBe('notes.txt · text');
        // Runs and Recent filter the search: Browse has neither.
        expect(screen.queryByRole('button', { name: 'Runs' })).toBeNull();
        expect(option('_runs')).toHaveAttribute('aria-selected', 'true');
        expect(primary()).toHaveTextContent('Open ‘_runs’');
        expect(hints()).toHaveTextContent('esc close');
        selectRow('notes.txt');
        expect(primary()).toHaveTextContent('Options for ‘notes.txt’');
        selectRow('New folder here');
        expect(primary()).toHaveTextContent('New folder here');
        fireEvent.keyDown(field(), { key: 'Tab' });
        expect(activeMode()).toBe('Search');
    });

    it('typing filters the folder\'s entries by name; the creation rows always stay (P03)', async () => {
        await mount();
        toBrowse();
        fireEvent.change(field(), { target: { value: 'd' } });
        expect(names()).toEqual(['docs', 'selected.md', 'New file here', 'New folder here']);
        fireEvent.change(field(), { target: { value: '*.txt' } });
        expect(names()).toEqual(['notes.txt', 'New file here', 'New folder here']);
        fireEvent.change(field(), { target: { value: 'nothing-here' } });
        expect(names()).toEqual(['New file here', 'New folder here']);
        fireEvent.keyDown(field(), { key: 'ArrowDown' });
        expect(option('New folder here')).toHaveAttribute('aria-selected', 'true');
    });

    it('↵ or a click enters a folder with the field cleared; Esc goes up on the folder just left, and closes at the root (P03)', async () => {
        await mount();
        toBrowse();
        fireEvent.change(field(), { target: { value: 'p' } });
        expect(names()).toEqual(['pkg', 'New file here', 'New folder here']);
        expect(primary()).toHaveTextContent('Open ‘pkg’');
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(location()).toHaveTextContent('pkg');
        expect(field()).toHaveValue('');
        expect(names()).toEqual(['view.renderer', 'New file here', 'New folder here']);
        expect(description('view.renderer')).toBe('pkg/view.renderer · renderer');
        expect(option('view.renderer')).toHaveAttribute('aria-selected', 'true');
        expect(hints()).toHaveTextContent('esc up a folder');
        fireEvent.keyDown(field(), { key: 'Escape' });
        expect(location()).toHaveTextContent('root');
        expect(names()).toEqual(ROOT);
        expect(option('pkg')).toHaveAttribute('aria-selected', 'true');
        // A click enters too; nothing is created inside a run's folder.
        fireEvent.click(option('_runs'));
        expect(location()).toHaveTextContent('_runs');
        expect(names()).toEqual(['r-00000000', 'New file here', 'New folder here']);
        expect(option('New file here')).toHaveAttribute('aria-disabled', 'true');
        expect(option('New file here')).toHaveTextContent('belong to runs');
        fireEvent.keyDown(field(), { key: 'Escape' });
        expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
        fireEvent.keyDown(field(), { key: 'Escape' });
        expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        expect(screen.queryByRole('dialog', { name: 'Files' })).toBeNull();
    });

    it('New file here turns the field into the name: the extension completes as in the small file creator, ↵ creates in the browsed folder (P03)', async () => {
        await seatRenderers([{ id: 'markdown', name: 'Markdown view', extensions: ['.md', '.markdown'] }, { id: 'text', name: 'Text', extensions: ['.txt'], default_for: ['*'] }]);
        try {
            await mount();
            toBrowse();
            enterFolder('pkg');
            selectRow('New file here');
            expect(primary()).toHaveTextContent('New file here');
            fireEvent.keyDown(field(), { key: 'Enter' });
            const name = screen.getByRole('combobox', { name: 'Name the new file' });
            expect(name).toHaveValue('');
            expect(document.activeElement).toBe(name);
            expect(screen.queryByRole('listbox')).toBeNull();
            expect(location()).toHaveTextContent('pkg');
            expect(primary()).toBeDisabled();
            expect(primary()).toHaveAttribute('title', 'Name the new file');
            expect(hints()).toHaveTextContent('esc cancel');
            // Tab accepts a completion while the field names the file: the
            // other mode waits, and no hint offers it.
            expect(screen.queryByText('to toggle')).toBeNull();
            expect(modeTabs()[0]).toBeDisabled();
            expect(modeTabs()[0]).toHaveAttribute('title', 'Create or cancel the new file first');

            fireEvent.change(name, { target: { value: 'notes.m' } });
            const completions = filenameCompletions('notes.m', getSupportedExtensions());
            expect(completions).toEqual(expect.arrayContaining(['notes.md', 'notes.markdown']));
            const ghost = () => document.querySelector('[data-filename-completion]');
            const note = () => screen.getByTestId('cell-panel-field-note');
            expect(ghost()).toHaveTextContent(completions[0]);
            expect(name).toHaveAttribute('aria-autocomplete', 'inline');
            expect(note()).toHaveTextContent(`Default renderer: ${resolveRenderer('notes.m').name} · Tab to accept · ↑↓ to change extension`);
            // ↑↓ cycle the completions, never the list or the modes.
            fireEvent.keyDown(name, { key: 'ArrowDown' });
            expect(ghost()).toHaveTextContent(completions[1]);
            fireEvent.keyDown(name, { key: 'ArrowUp' });
            expect(ghost()).toHaveTextContent(completions[0]);
            for (let at = 0; at < completions.indexOf('notes.md'); at++) fireEvent.keyDown(name, { key: 'ArrowDown' });
            fireEvent.keyDown(name, { key: 'Tab' });
            expect(name).toHaveValue('notes.md');
            expect(ghost()).toBeNull();
            expect(activeMode()).toBe('Browse/Create');
            expect(note()).toHaveTextContent('Default renderer: Markdown view');
            expect(note()).not.toHaveTextContent('Tab to accept');
            expect(primary()).toHaveTextContent('Create ‘notes.md’');

            await act(async () => { fireEvent.keyDown(name, { key: 'Enter' }); });
            expect(filesNow.map(f => f.name)).toContain('pkg/notes.md');
            expect(location()).toHaveTextContent('pkg');
            expect(field()).toHaveAccessibleName('Filter this folder');
            expect(field()).toHaveValue('');
            expect(names()).toEqual(['notes.md', 'view.renderer', 'New file here', 'New folder here']);
            expect(option('notes.md')).toHaveAttribute('aria-selected', 'true');
            expect(screen.getByRole('status')).toHaveTextContent('Created ‘notes.md’.');
            expect(screen.getByText('to toggle')).toBeInTheDocument();
            expect(modeTabs()[0]).toBeEnabled();
        } finally { await seatRenderers([]); }
    });

    it('a name the folder refuses stays in the field with the reason; Esc cancels back to the folder, the panel open (P03)', async () => {
        await mount();
        toBrowse();
        fireEvent.click(option('New file here'));
        const name = field();
        fireEvent.change(name, { target: { value: 'a/b.txt' } });
        await act(async () => { fireEvent.keyDown(name, { key: 'Enter' }); });
        expect(screen.getByRole('alert')).toHaveTextContent('A name can’t contain /');
        expect(name).toHaveValue('a/b.txt');
        fireEvent.change(name, { target: { value: 'notes.txt' } });
        expect(screen.queryByRole('alert')).toBeNull();
        await act(async () => { fireEvent.keyDown(name, { key: 'Enter' }); });
        expect(screen.getByRole('alert')).toHaveTextContent('already exists');
        expect(name).toHaveValue('notes.txt');
        expect(spies.update).not.toHaveBeenCalled();
        fireEvent.keyDown(name, { key: 'Escape' });
        expect(field()).toHaveAccessibleName('Filter this folder');
        expect(names()).toEqual(ROOT);
        expect(option('New file here')).toHaveAttribute('aria-selected', 'true');
        expect(activeMode()).toBe('Browse/Create');
        expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
        // Esc from elsewhere in the panel cancels the naming too.
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(field()).toHaveAccessibleName('Name the new file');
        fireEvent.keyDown(panel(), { key: 'Escape' });
        expect(field()).toHaveAccessibleName('Filter this folder');
        expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
    });

    it('a click on New folder here names a folder a .collkeep keeps, listed and selected where it was made (P03)', async () => {
        await mount();
        toBrowse();
        fireEvent.click(option('New folder here'));
        const name = screen.getByRole('combobox', { name: 'Name the new folder' });
        expect(document.activeElement).toBe(name);
        expect(screen.queryByTestId('cell-panel-field-note')).toBeNull();
        fireEvent.change(name, { target: { value: 'assets' } });
        expect(primary()).toHaveTextContent('Create ‘assets’');
        // A press on the primary leaves the keyboard in the field.
        expect(fireEvent.mouseDown(primary())).toBe(false);
        await act(async () => { fireEvent.click(primary()); });
        expect(filesNow.map(f => f.name)).toContain('assets/.collkeep');
        expect(names()).toEqual(['_runs', 'assets', 'docs', 'pkg', 'notes.txt', 'selected.md', 'New file here', 'New folder here']);
        expect(option('assets')).toHaveAttribute('aria-selected', 'true');
        expect(screen.getByRole('status')).toHaveTextContent('Created ‘assets’.');
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(location()).toHaveTextContent('assets');
        expect(names()).toEqual(['New file here', 'New folder here']);
        // What was said goes once the list moves elsewhere.
        expect(screen.queryByRole('status')).toBeNull();
    });

    it('on a built-in card nothing is created: both creation rows are refused with the reason (P03)', async () => {
        await mount({ builtin: true });
        toBrowse();
        for (const label of ['New file here', 'New folder here']) {
            expect(option(label)).toHaveAttribute('aria-disabled', 'true');
            expect(option(label)).toHaveTextContent('Built-in packages are part of the app');
        }
        // A click on a refused row only selects it.
        fireEvent.click(option('New file here'));
        expect(option('New file here')).toHaveAttribute('aria-selected', 'true');
        expect(screen.queryByRole('combobox', { name: 'Name the new file' })).toBeNull();
        expect(primary()).toBeDisabled();
        expect(primary()).toHaveAttribute('title', 'Built-in packages are part of the app');
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(screen.queryByRole('combobox', { name: 'Name the new file' })).toBeNull();
    });
});

describe('FilesPanel: a file\'s options', () => {
    it('↵ on a file opens its options: one keyboard list through Open, Open with…, Info, Duplicate, Rename and Delete (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await mount({ initial: [vf('a.txt', 'a'), vf('b.txt', 'b')], active: null });
            fireEvent.keyDown(search(), { key: 'Enter' });
            expect(optionsList()).toHaveAccessibleName('‘a.txt’');
            expect(document.activeElement).toBe(optionsList());
            expect(screen.queryAllByRole('heading')).toEqual([]);
            expect(entryLabels()).toEqual(['Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
            expect(entry('Open')).toHaveTextContent('with Zed notes');
            expect(entry('Open with…')).toHaveTextContent('Choose any renderer');
            expect(selectedEntry()).toBe('Open');
            expect(optionsList()).toHaveAttribute('aria-activedescendant', entry('Open').id);
            fireEvent.keyDown(optionsList(), { key: 'ArrowUp' });
            expect(selectedEntry()).toBe('Delete…');
            fireEvent.keyDown(optionsList(), { key: 'ArrowDown' });
            fireEvent.keyDown(optionsList(), { key: 'ArrowDown' });
            expect(selectedEntry()).toBe('Open with…');
            // ↵ runs the selected entry: Open with… opens its step, the panel stays.
            fireEvent.keyDown(optionsList(), { key: 'Enter' });
            expect(rendererList()).toHaveAccessibleName('Open ‘a.txt’ with');
            expect(document.activeElement).toBe(rendererList());
            expect(spies.openWith).not.toHaveBeenCalled();
            expect(spies.open).not.toHaveBeenCalled();
            expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
        } finally { await seatRenderers([]); }
    });

    it('with an Open with override on the file\'s tab, Open keeps it (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await mount({ initial: [vf('a.txt', 'a')], active: 'a.txt', tabs: l => openFileTab(l, 'a.txt', null, 'alpha') });
            fireEvent.keyDown(search(), { key: 'Enter' });
            expect(entry('Open')).toHaveTextContent('with Alpha view');
            fireEvent.keyDown(optionsList(), { key: 'Enter' });
            expect(spies.open).toHaveBeenCalledExactlyOnceWith('a.txt');
            expect(spies.openWith).not.toHaveBeenCalled();
        } finally { await seatRenderers([]); }
    });

    it('Open opens the file and closes the panel, by ↵ or a click; a click on a row opens its options, and a double-click runs none of them (P03)', async () => {
        await mount();
        fireEvent.click(option('notes.txt'), { detail: 1 });
        expect(optionsList()).toHaveAccessibleName('‘notes.txt’');
        // The double-click's second press lands on the entry under it.
        fireEvent.click(entry('Open'), { detail: 2 });
        fireEvent.doubleClick(entry('Open'));
        expect(spies.open).not.toHaveBeenCalled();
        expect(optionsList()).toHaveAccessibleName('‘notes.txt’');
        fireEvent.keyDown(optionsList(), { key: 'Enter' });
        expect(spies.open).toHaveBeenCalledExactlyOnceWith('notes.txt');
        expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        cleanup();
        await mount();
        toBrowse();
        enterFolder('pkg');
        fireEvent.keyDown(field(), { key: 'Enter' });
        fireEvent.click(entry('Open'));
        expect(spies.open).toHaveBeenLastCalledWith('pkg/view.renderer');
        expect(spies.openWith).not.toHaveBeenCalled();
    });

    it('Esc or Back returns to the list exactly as it was: the mode, the folder, the filter and the selection (P03)', async () => {
        await mount();
        toBrowse();
        enterFolder('pkg');
        fireEvent.change(field(), { target: { value: 'view' } });
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(optionsList()).toHaveAccessibleName('‘view.renderer’');
        fireEvent.keyDown(optionsList(), { key: 'Escape' });
        expect(activeMode()).toBe('Browse/Create');
        expect(location()).toHaveTextContent('pkg');
        expect(field()).toHaveValue('view');
        expect(option('view.renderer')).toHaveAttribute('aria-selected', 'true');
        expect(document.activeElement).toBe(field());
        fireEvent.keyDown(field(), { key: 'Enter' });
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        expect(location()).toHaveTextContent('pkg');
        expect(field()).toHaveValue('view');
        expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
    });

    it('on a built-in card a file only opens and informs: Duplicate, Rename and Delete are refused with the reason (P03)', async () => {
        await mount({ builtin: true });
        optionsOf('view.renderer');
        // A built-in card's packages are the app's: nothing to approve.
        expect(screen.queryAllByRole('heading')).toEqual([]);
        for (const label of ['Duplicate…', 'Rename…', 'Delete…']) {
            expect(entry(label)).toHaveAttribute('aria-disabled', 'true');
            expect(entry(label)).toHaveTextContent('Built-in packages are part of the app');
        }
        for (const label of ['Open', 'Info']) expect(entry(label)).not.toHaveAttribute('aria-disabled');
        fireEvent.click(entry('Delete…'));
        fireEvent.click(entry('Rename…'));
        expect(screen.queryByText(/^Delete ‘view.renderer’\?/)).toBeNull();
        expect(screen.queryByRole('textbox', { name: 'New name' })).toBeNull();
        expect(spies.update).not.toHaveBeenCalled();
    });

    it('a file a live run holds is not duplicated, renamed or deleted: each says which run holds it (P03)', async () => {
        await mount();
        optionsOf('log.txt');
        for (const label of ['Duplicate…', 'Rename…', 'Delete…']) {
            expect(entry(label)).toHaveAttribute('aria-disabled', 'true');
            expect(entry(label).textContent).toMatch(/Held by .* until that run stops/);
        }
        for (let at = 0; at < entries().length - 1; at++) fireEvent.keyDown(optionsList(), { key: 'ArrowDown' });
        expect(selectedEntry()).toBe('Delete…');
        fireEvent.keyDown(optionsList(), { key: 'Enter' });
        expect(screen.queryByText(/^Delete ‘log.txt’\?/)).toBeNull();
        expect(spies.update).not.toHaveBeenCalled();
    });

    it('a finished run\'s file is duplicated or deleted, but keeps the name its run gave it (P03)', async () => {
        runLive = false;
        await mount();
        optionsOf('log.txt');
        expect(entry('Rename…')).toHaveAttribute('aria-disabled', 'true');
        expect(entry('Rename…')).toHaveTextContent('A run’s files keep their names');
        for (const label of ['Duplicate…', 'Delete…']) expect(entry(label)).not.toHaveAttribute('aria-disabled');
    });
});

describe('FilesPanel: Open with…', () => {
    /** Open the options of the only file and, from them, Open with…. */
    const toOpenWith = async (props: Parameters<typeof Harness>[0] = {}) => {
        await mount({ initial: [vf('a.txt', 'a')], active: null, ...props });
        fireEvent.keyDown(search(), { key: 'Enter' });
        fireEvent.click(entry('Open with…'), { detail: 1 });
    };

    it('lists the renderers that claim the file in the right-click\'s order, then every other renderer by name; ↵ opens with the selected one and closes the panel (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await toOpenWith();
            expect(screen.getByText('Open ‘a.txt’ with')).toBeInTheDocument();
            expect(document.activeElement).toBe(rendererList());
            expect(headings()).toEqual(['For this file', 'Other renderers']);
            expect(sectionNames('For this file')).toEqual(rendererCandidatesFor('a.txt').map(renderer => renderer.name));
            expect(sectionNames('For this file')).toEqual(['Zed notes', 'Alpha view', 'Text']);
            expect(sectionNames('Other renderers')).toEqual(['Bar chart', 'Wall board']);
            // No tab shows the file: its default is marked, nothing is current.
            expect(rendererNote('Zed notes')).toBe('default');
            for (const name of ['Alpha view', 'Text', 'Bar chart', 'Wall board']) expect(rendererNote(name)).toBeUndefined();
            expect(selectedRenderer()).toBe('Zed notes');
            expect(rendererList()).toHaveAttribute('aria-activedescendant', renderer('Zed notes').id);
            fireEvent.keyDown(rendererList(), { key: 'ArrowUp' });
            expect(selectedRenderer()).toBe('Wall board');
            fireEvent.keyDown(rendererList(), { key: 'ArrowDown' });
            expect(selectedRenderer()).toBe('Zed notes');
            fireEvent.keyDown(rendererList(), { key: 'ArrowUp' });
            fireEvent.keyDown(rendererList(), { key: 'ArrowUp' });
            expect(selectedRenderer()).toBe('Bar chart');
            fireEvent.keyDown(rendererList(), { key: 'Enter' });
            expect(spies.openWith).toHaveBeenCalledExactlyOnceWith('a.txt', 'chart');
            expect(spies.open).not.toHaveBeenCalled();
            expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        } finally { await seatRenderers([]); }
    });

    it('with an override on the file\'s tab, its renderer is current and the default opens with none, clearing it (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await toOpenWith({ active: 'a.txt', tabs: l => openFileTab(l, 'a.txt', null, 'alpha') });
            expect(rendererNote('Zed notes')).toBe('default');
            expect(rendererNote('Alpha view')).toBe('current');
            fireEvent.keyDown(rendererList(), { key: 'Enter' });
            expect(spies.openWith).toHaveBeenCalledExactlyOnceWith('a.txt', undefined);
            expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        } finally { await seatRenderers([]); }
    });

    it('with the tab on its default, the default is both; a click opens with that renderer\'s id and closes the panel (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await toOpenWith({ active: 'a.txt', tabs: l => openFileTab(l, 'a.txt', null) });
            expect(rendererNote('Zed notes')).toBe('default · current');
            expect(rendererNote('Alpha view')).toBeUndefined();
            fireEvent.click(renderer('Alpha view'), { detail: 1 });
            expect(spies.openWith).toHaveBeenCalledExactlyOnceWith('a.txt', 'alpha');
            expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        } finally { await seatRenderers([]); }
    });

    it('a tab showing a renderer that does not claim the file has it current among the others (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await toOpenWith({ active: 'a.txt', tabs: l => openFileTab(l, 'a.txt', null, 'board') });
            expect(sectionNames('Other renderers')).toEqual(['Bar chart', 'Wall board']);
            expect(rendererNote('Wall board')).toBe('current');
            expect(rendererNote('Zed notes')).toBe('default');
            fireEvent.click(renderer('Zed notes'));
            expect(spies.openWith).toHaveBeenCalledExactlyOnceWith('a.txt', undefined);
        } finally { await seatRenderers([]); }
    });

    it.each([
        { scenario: 'a Unicode case-folded renderer suffix', aliases: [] as HandlerInfo[], rendererExt: '.STRASSE' },
        { scenario: 'a handler\'s rendered-as alias', aliases: [{ ...handlerInfo('german', ['.strasse']), types: [{ ext: '.strasse', render_as: '.html' }] }], rendererExt: '.HTML' },
    ])('lists for the file the renderers its right-click offers, for $scenario; the rest are the others (P03)', async ({ aliases, rendererExt }) => {
        catalog = aliases;
        await seatRenderers([
            { id: 'matching', name: 'Matching renderer', extensions: [rendererExt] },
            { id: 'alternate', name: 'Alternate renderer', extensions: [rendererExt.toLowerCase()] },
            { id: 'other', name: 'Other renderer', extensions: ['.txt'] },
            { id: 'text', name: 'Text', extensions: ['.txt'], default_for: ['*'] },
        ]);
        try {
            await toOpenWith({ initial: [vf('sample.straße', 'bytes')] });
            // The registry's claims and the plain-text fallback, as the right-click offers them.
            expect(rendererCandidatesFor('sample.straße').map(renderer => renderer.name)).toEqual(['Matching renderer', 'Alternate renderer', 'Text']);
            expect(sectionNames('For this file')).toEqual(['Matching renderer', 'Alternate renderer', 'Text']);
            expect(sectionNames('Other renderers')).toEqual(['Other renderer']);
            fireEvent.click(renderer('Alternate renderer'));
            expect(spies.openWith).toHaveBeenCalledExactlyOnceWith('sample.straße', 'alternate');
            expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        } finally { catalog = []; await seatRenderers([]); }
    });

    it('leaves Other renderers out when every renderer in the registry claims the file (P03)', async () => {
        await seatRenderers([{ id: 'text', name: 'Text', extensions: ['.txt'], default_for: ['*'] }, { id: 'alpha', name: 'Alpha view', extensions: ['.txt'] }]);
        try {
            await toOpenWith();
            expect(headings()).toEqual(['For this file']);
            expect(sectionNames('For this file')).toEqual(['Text', 'Alpha view']);
        } finally { await seatRenderers([]); }
    });

    it('Esc or Back returns to the options on Open with…, so ↵ comes back; the second press of the double-click that opened the step runs nothing (P03)', async () => {
        await seatRenderers(RENDERERS);
        try {
            await toOpenWith();
            fireEvent.click(renderer('Zed notes'), { detail: 2 });
            fireEvent.doubleClick(renderer('Zed notes'));
            expect(spies.openWith).not.toHaveBeenCalled();
            fireEvent.keyDown(rendererList(), { key: 'Escape' });
            expect(optionsList()).toHaveAccessibleName('‘a.txt’');
            expect(document.activeElement).toBe(optionsList());
            expect(selectedEntry()).toBe('Open with…');
            fireEvent.keyDown(optionsList(), { key: 'Enter' });
            expect(selectedRenderer()).toBe('Zed notes');
            fireEvent.click(screen.getByRole('button', { name: 'Back' }));
            expect(optionsList()).toHaveAccessibleName('‘a.txt’');
            expect(selectedEntry()).toBe('Open with…');
            fireEvent.keyDown(optionsList(), { key: 'Escape' });
            expect(search()).toBeInTheDocument();
            expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
            expect(spies.openWith).not.toHaveBeenCalled();
        } finally { await seatRenderers([]); }
    });
});

describe('FilesPanel: Duplicate, Rename and Delete', () => {
    it('Duplicate asks for a name, prefilled with a free one beside the file, refuses a bad one and copies to the typed path, back on the list (P03)', async () => {
        await mount({ initial: [vf('notes.txt', 'notes'), vf('notes-copy.txt', 'taken')] });
        optionsOf('notes.txt');
        fireEvent.click(entry('Duplicate…'));
        const name = screen.getByRole<HTMLInputElement>('textbox', { name: 'Duplicate name' });
        expect(name).toHaveValue('notes-copy-2.txt');
        expect(document.activeElement).toBe(name);
        expect([name.selectionStart, name.selectionEnd]).toEqual([0, 'notes-copy-2.txt'.length]);
        const submit = screen.getByRole('button', { name: 'Duplicate' });
        for (const [bad, reason] of [
            ['/abs.txt', /relative path/], ['a\\b.txt', /relative path/], ['a//b.txt', /relative path/], ['./b.txt', /relative path/],
            ['../b.txt', /relative path/], ['_runs/r-1/b.txt', /outside run folders/], ['notes-copy.txt', /already exists/],
        ] as const) {
            fireEvent.change(name, { target: { value: bad } });
            await act(async () => { fireEvent.click(submit); });
            expect(screen.getByRole('alert').textContent).toMatch(reason);
        }
        fireEvent.change(name, { target: { value: '' } });
        expect(submit).toBeDisabled();
        expect(spies.update).not.toHaveBeenCalled();
        // Back returns to the file's options, on Duplicate….
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        expect(optionsList()).toHaveAccessibleName('‘notes.txt’');
        expect(selectedEntry()).toBe('Duplicate…');
        fireEvent.click(entry('Duplicate…'));
        fireEvent.change(screen.getByRole('textbox', { name: 'Duplicate name' }), { target: { value: 'kept/notes-2.txt' } });
        expect(screen.queryByRole('alert')).toBeNull();
        await act(async () => { fireEvent.submit(screen.getByRole('textbox', { name: 'Duplicate name' }).closest('form')!); });
        expect(fixtureText(filesNow.find(f => f.name === 'kept/notes-2.txt'))).toBe('notes');
        expect(filesNow.map(f => f.name)).not.toContain('notes-copy-2.txt');
        expect(screen.getByRole('status')).toHaveTextContent('Duplicated as ‘notes-2.txt’.');
        expect(search()).toBeInTheDocument();
        expect(option('notes-2.txt')).toHaveAttribute('aria-selected', 'true');
    });

    it('Duplicate re-checks the run lock: a file a run took while its name was typed is refused (P03)', async () => {
        await mount({ initial: [vf('notes.txt', 'notes')], active: null });
        optionsOf('notes.txt');
        fireEvent.click(entry('Duplicate…'));
        held.add('notes.txt');
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Duplicate' })); });
        expect(screen.getByRole('alert')).toHaveTextContent('The file changed. Retry.');
        expect(spies.update).not.toHaveBeenCalled();
    });

    it('Rename asks for the new name with the stem selected, refuses a bad one in place and renames through the file operations, back on the list (P03)', async () => {
        await mount({ initial: [vf('docs/notes.txt', 'notes'), vf('docs/other.md', 'other'), vf('docs/sub/deep.txt', 'deep')], active: null });
        optionsOf('notes.txt');
        fireEvent.click(entry('Rename…'));
        expect(screen.getByText('Rename ‘notes.txt’')).toBeInTheDocument();
        const name = screen.getByRole<HTMLInputElement>('textbox', { name: 'New name' });
        expect(name).toHaveValue('notes.txt');
        expect(document.activeElement).toBe(name);
        // Only the stem is selected, so typing keeps the extension.
        expect([name.selectionStart, name.selectionEnd]).toEqual([0, 'notes'.length]);
        const submit = screen.getByRole('button', { name: 'Rename' });
        for (const [bad, reason] of [
            ['a/b.txt', 'A name can’t contain /'], ['a\\b.txt', 'A name can’t contain /'], ['..', 'Invalid name'],
            ['.collkeep', 'Reserved name'], ['other.md', '‘docs/other.md’ already exists.'], ['sub', '‘docs/sub’ already exists.'],
        ] as const) {
            fireEvent.change(name, { target: { value: bad } });
            await act(async () => { fireEvent.click(submit); });
            expect(screen.getByRole('alert')).toHaveTextContent(reason);
        }
        expect(spies.update).not.toHaveBeenCalled();
        // The same name renames nothing: back to the file's options.
        fireEvent.change(name, { target: { value: 'notes.txt' } });
        await act(async () => { fireEvent.submit(name.closest('form')!); });
        expect(optionsList()).toHaveAccessibleName('‘notes.txt’');
        expect(spies.update).not.toHaveBeenCalled();
        fireEvent.click(entry('Rename…'));
        const renamed = screen.getByRole('textbox', { name: 'New name' });
        fireEvent.change(renamed, { target: { value: 'renamed.md' } });
        await act(async () => { fireEvent.submit(renamed.closest('form')!); });
        // The write names the move, so the cell's tabs and documents follow it.
        expect(spies.update).toHaveBeenCalledTimes(1);
        expect(spies.update.mock.calls[0][1]).toEqual({ renames: [{ source: 'docs/notes.txt', target: 'docs/renamed.md' }] });
        expect(filesNow.map(f => f.name)).toEqual(['docs/renamed.md', 'docs/other.md', 'docs/sub/deep.txt']);
        expect(screen.getByRole('status')).toHaveTextContent('Renamed to ‘renamed.md’.');
        expect(search()).toBeInTheDocument();
        expect(option('renamed.md')).toHaveAttribute('aria-selected', 'true');
    });

    it('Delete asks under the options; Keep or Esc goes back to them, Delete removes the file and the list comes back on its neighbour (P03)', async () => {
        await mount({ initial: [vf('a.txt'), vf('b.txt'), vf('c.txt')], active: null });
        optionsOf('b.txt');
        fireEvent.click(entry('Delete…'));
        const strip = () => screen.getByText('Delete ‘b.txt’?').parentElement as HTMLElement;
        expect(optionsList()).toBeInTheDocument();
        fireEvent.click(within(strip()).getByRole('button', { name: 'Keep' }));
        expect(screen.queryByText('Delete ‘b.txt’?')).toBeNull();
        expect(optionsList()).toBeInTheDocument();
        await waitFor(() => expect(document.activeElement).toBe(optionsList()));
        fireEvent.click(entry('Delete…'));
        fireEvent.keyDown(panel(), { key: 'Escape' });
        expect(screen.queryByText('Delete ‘b.txt’?')).toBeNull();
        expect(optionsList()).toHaveAccessibleName('‘b.txt’');
        expect(spies.update).not.toHaveBeenCalled();
        fireEvent.click(entry('Delete…'));
        await act(async () => { fireEvent.click(within(strip()).getByRole('button', { name: 'Delete' })); });
        expect(filesNow.map(f => f.name)).toEqual(['a.txt', 'c.txt']);
        expect(search()).toBeInTheDocument();
        await waitFor(() => expect(option('c.txt')).toHaveAttribute('aria-selected', 'true'));
        expect(screen.getByRole('status')).toHaveTextContent('Deleted ‘b.txt’.');
    });

    it('Delete in Browse comes back on the neighbour in the folder; an emptied folder stays (P03)', async () => {
        await mount({ initial: [vf('docs/a.txt'), vf('docs/b.txt'), vf('top.txt')], active: null });
        toBrowse();
        enterFolder('docs');
        optionsOf('b.txt');
        fireEvent.click(entry('Delete…'));
        await act(async () => { fireEvent.click(within(screen.getByText('Delete ‘b.txt’?').parentElement as HTMLElement).getByRole('button', { name: 'Delete' })); });
        expect(location()).toHaveTextContent('docs');
        expect(names()).toEqual(['a.txt', 'New file here', 'New folder here']);
        expect(option('a.txt')).toHaveAttribute('aria-selected', 'true');
        fireEvent.keyDown(field(), { key: 'Enter' });
        fireEvent.click(entry('Delete…'));
        await act(async () => { fireEvent.click(within(screen.getByText('Delete ‘a.txt’?').parentElement as HTMLElement).getByRole('button', { name: 'Delete' })); });
        expect(filesNow.map(f => f.name)).toEqual(['top.txt', 'docs/.collkeep']);
        expect(names()).toEqual(['New file here', 'New folder here']);
    });
});

/** Put the catalog's handler cells in the store beside the work cell. */
function seatHandlers(entries: HandlerInfo[]) {
    catalog = entries;
    const targets: Cell[] = [...new Set(entries.map(entry => entry.cell_id))].map(id => ({
        id, name: id, files: entries.filter(entry => entry.cell_id === id).map(entry => vf(entry.definition_path, `id: ${entry.id}\nname: ${entry.name}\n`)),
    }));
    const next = { ...channel, main_stacks: [{ ...channel.main_stacks[0], cells: targets }, channel.main_stacks[1]] };
    useCollimateStore.setState(s => ({ activeChannel: next, state: { active_channel_id: channel.id, channels: [next] }, cellCache: { ...s.cellCache, ...Object.fromEntries(targets.map(c => [c.id, c])) } }));
}

describe('FilesPanel: Info', () => {
    it('shows the file\'s path, folder, size, type, last edit and arrival, its handler and the journal\'s recent changes naming it (P03)', async () => {
        const changes = [
            journalEvent('2026-10-05T10:00:00Z', 'run', { run: journalRun({ note: 'Wrote the notes', files_written: ['docs/notes.txt'] }) }),
            journalEvent('2026-10-04T09:00:00Z', 'edit', { files: ['docs/other.md'] }),
            journalEvent('2026-10-03T08:00:00Z', 'edit', { files: ['docs/notes.txt'] }),
            journalEvent('2026-10-01T07:00:00Z', 'make', { files: [] }),
        ];
        journalPage = changes;
        await info([vf('docs/notes.txt', 'notes ✓', '2026-10-05T10:00:00Z')]);
        expect(screen.getByText('Info on ‘notes.txt’')).toBeInTheDocument();
        expect(headings()).toEqual(['File', 'Handler', 'Recent changes']);
        expect(fact('Path')).toBe('docs/notes.txt');
        expect(fact('Folder')).toBe('docs');
        // UTF-8 bytes, read from the file's pinned body.
        await waitFor(() => expect(fact('Size')).toBe(formatBytes(9)));
        expect(fact('Type')).toBe('text');
        expect(fact('Last edited')).toBe(when('2026-10-05T10:00:00Z'));
        // The journal reaches the cell's making: the file's first event is its arrival.
        expect(fact('Created')).toBe(when('2026-10-03T08:00:00Z'));
        const recent = screen.getByRole('group', { name: 'Recent changes' });
        expect(within(recent).getAllByRole('listitem').map(item => item.textContent)).toEqual([
            `${eventLabel(changes[0])} · ${when(changes[0].t)}Wrote the notes`,
            `${eventLabel(changes[2])} · ${when(changes[2].t)}`,
        ]);
        expect(within(handlerSection()).getByText('No handler claims ‘.txt’ files.')).toBeInTheDocument();
        expect(requests.some(request => request.url.includes('/cells/cell/journal?'))).toBe(true);
    });

    it('a binary file\'s size comes from one ranged byte; a root file\'s folder is root; a partial journal gives an upper bound (P03)', async () => {
        serve(path => (path.includes('/cells/cell/files/image.png?') ? { ok: true, status: 206, headers: new Headers({ 'Content-Range': 'bytes 0-0/2048' }) } : undefined));
        journalPage = Array.from({ length: 100 }, (_, at) => journalEvent(`2026-10-0${at < 50 ? 5 : 4}T10:${String(at % 60).padStart(2, '0')}:00Z`, 'edit', { files: at >= 98 ? ['image.png'] : ['other.txt'] }));
        await info([vf('image.png', '', '', { encoding: 'binary', language: 'binary', body_hash: 'b'.repeat(64) })]);
        await waitFor(() => expect(fact('Size')).toBe(formatBytes(2048)));
        const ranged = requests.find(request => request.url.includes('/cells/cell/files/image.png?'));
        expect(new Headers(ranged?.init?.headers).get('Range')).toBe('bytes=0-0');
        expect(fact('Folder')).toBe('root');
        expect(fact('Type')).toBe('binary');
        expect(fact('Last edited')).toBe(when(journalPage[98].t));
        expect(fact('Created')).toBe(`On or before ${when(journalPage[99].t)}`);
    });

    it('in a cell a duplicate made, the arrival is only bounded; a newer cut file list bounds the last edit (P03)', async () => {
        journalPage = [
            journalEvent('2026-10-06T10:00:00Z', 'edit', { files: ['other.txt'], files_total: 150 }),
            journalEvent('2026-10-05T10:00:00Z', 'edit', { files: ['notes.txt'] }),
            journalEvent('2026-10-04T10:00:00Z', 'duplicate'),
        ];
        await info([vf('notes.txt', 'notes')]);
        await waitFor(() => expect(fact('Created')).toBe(`On or before ${when(journalPage[1].t)}`));
        expect(fact('Last edited')).toBe(`On or after ${when(journalPage[1].t)}`);
    });

    it('with no event naming the file it says so, and no arrival is claimed (P03)', async () => {
        journalPage = [journalEvent('2026-10-01T07:00:00Z', 'make')];
        await info([vf('notes.txt', 'notes')]);
        expect(await within(screen.getByRole('group', { name: 'Recent changes' })).findByText('No recorded change names this file.')).toBeInTheDocument();
        expect(within(screen.getByRole('group', { name: 'File' })).queryByText('Created')).toBeNull();
        expect(fact('Last edited')).toBe('Unknown');
    });

    it('a journal that cannot be loaded is the step\'s error, never "no change" (P03)', async () => {
        serve(path => (path.includes('/journal?') ? { ok: false, status: 500, json: async () => ({ detail: 'Journal down' }) } : undefined));
        await info([vf('notes.txt', 'notes')]);
        expect((await screen.findByRole('alert')).textContent).toMatch(/The changes could not be loaded: .*Journal down/);
        expect(fact('Last edited')).toBe('Unavailable');
        expect(screen.queryByRole('group', { name: 'Recent changes' })).toBeNull();
        expect(screen.queryByText('No recorded change names this file.')).toBeNull();
    });

    const RECORD: ProvenanceRecord = {
        folder: 'code-editor', source_kind: 'bundled', source_id: '/abs/path/content', package_kind: 'renderer', package_id: 'code-editor',
        package_version: '1.1.0', content_hash: 'ab12cd34ef56'.padEnd(64, '7'), sdk_version: '0.1.0', admitted_at: '2026-10-26T10:00:00.000000Z',
    };
    const provenanceFact = (label: string) => within(screen.getByRole('group', { name: 'Provenance' })).getByText(label).nextElementSibling;

    it('a file in a recorded package folder shows where the package came from, its hash short and in full on hover (P03)', async () => {
        provenance = { sdk_version: '0.1.0', records: [RECORD] };
        await info([vf('code-editor/main.tsx', 'x')]);
        const section = await screen.findByRole('group', { name: 'Provenance' });
        expect(Array.from(section.querySelectorAll('dt')).map(dt => dt.textContent)).toEqual(['Source', 'Package', 'Version', 'Content hash', 'SDK']);
        expect(provenanceFact('Source')?.textContent).toBe('bundled · /abs/path/content');
        expect(within(provenanceFact('Source') as HTMLElement).getByText('/abs/path/content')).toHaveClass('font-mono');
        expect(provenanceFact('Package')?.textContent).toBe('code-editor (renderer)');
        expect(provenanceFact('Version')?.textContent).toBe('1.1.0');
        const hash = within(provenanceFact('Content hash') as HTMLElement).getByText('ab12cd34ef56');
        expect(hash).toHaveClass('font-mono');
        expect(hash).toHaveAttribute('title', RECORD.content_hash);
        expect(provenanceFact('SDK')?.textContent).toBe('0.1.0');
        expect(within(section).queryByText(/Admitted under SDK/)).toBeNull();
        expect(requests.some(request => request.url.endsWith('/cells/cell/provenance'))).toBe(true);
    });

    it('a zip source shows the kind alone, and a package with no version says None (P03)', async () => {
        provenance = { sdk_version: '0.1.0', records: [{ ...RECORD, source_kind: 'zip', source_id: 'zip', package_version: null }] };
        await info([vf('code-editor/main.tsx', 'x')]);
        await screen.findByRole('group', { name: 'Provenance' });
        expect(provenanceFact('Source')?.textContent).toBe('zip');
        expect(provenanceFact('Version')?.textContent).toBe('None');
    });

    it('a record admitted under another SDK says so under the facts (P03)', async () => {
        provenance = { sdk_version: '0.2.0', records: [RECORD] };
        await info([vf('code-editor/main.tsx', 'x')]);
        const section = await screen.findByRole('group', { name: 'Provenance' });
        expect(within(section).getByText('Admitted under SDK 0.1.0; this app runs SDK 0.2.0.')).toBeInTheDocument();
    });

    it('a file outside every recorded folder, or a cell with no record, shows no Provenance (P03)', async () => {
        provenance = { sdk_version: '0.1.0', records: [RECORD] };
        await info([vf('code-editor-notes.txt', 'x')]);
        await waitFor(() => expect(requests.some(request => request.url.endsWith('/cells/cell/provenance'))).toBe(true));
        await waitFor(() => expect(fact('Size')).not.toBe('Loading…'));
        expect(screen.queryByRole('group', { name: 'Provenance' })).toBeNull();
        cleanup();
        provenance = { sdk_version: '0.1.0', records: [] };
        await info([vf('code-editor/main.tsx', 'x')]);
        await waitFor(() => expect(fact('Size')).not.toBe('Loading…'));
        expect(screen.queryByRole('group', { name: 'Provenance' })).toBeNull();
    });

    it('a provenance that cannot be loaded is a note in its section (P03)', async () => {
        serve(path => (path.endsWith('/provenance') ? { ok: false, status: 500, json: async () => ({ detail: 'Provenance down' }) } : undefined));
        await info([vf('code-editor/main.tsx', 'x')]);
        expect((await within(await screen.findByRole('group', { name: 'Provenance' })).findByRole('alert')).textContent).toMatch(/The provenance could not be loaded: .*Provenance down/);
    });

    it('Esc or ↵ on the step goes back to the options on Info, and Esc again to the list as it was (P03)', async () => {
        await mount({ initial: [vf('docs/a.txt', 'a'), vf('docs/b.txt', 'b')], active: null });
        toBrowse();
        enterFolder('docs');
        optionsOf('b.txt');
        await act(async () => { fireEvent.click(entry('Info')); });
        const body = screen.getByRole('group', { name: 'Info on ‘b.txt’' });
        expect(document.activeElement).toBe(body);
        fireEvent.keyDown(body, { key: 'Enter' });
        expect(optionsList()).toHaveAccessibleName('‘b.txt’');
        expect(selectedEntry()).toBe('Info');
        await act(async () => { fireEvent.click(entry('Info')); });
        fireEvent.keyDown(screen.getByRole('group', { name: 'Info on ‘b.txt’' }), { key: 'Escape' });
        expect(optionsList()).toHaveAccessibleName('‘b.txt’');
        expect(selectedEntry()).toBe('Info');
        fireEvent.keyDown(optionsList(), { key: 'Escape' });
        expect(location()).toHaveTextContent('docs');
        expect(option('b.txt')).toHaveAttribute('aria-selected', 'true');
        expect(spies.open).not.toHaveBeenCalled();
    });

    it.each([
        { scenario: 'a Unicode case-folded suffix', path: 'sample.straße', entries: [handlerInfo('german', ['.strasse'])], owner: 'german' },
        { scenario: 'the unclashed type of a multi-type handler', path: 'sample.bar', entries: [handlerInfo('multi', ['.foo', '.bar'], ['.foo']), handlerInfo('other', ['.foo'], ['.foo'])], owner: 'multi' },
        { scenario: 'a clashing longest suffix blocking a shorter one', path: 'sample.long.foo', entries: [handlerInfo('long-a', ['.long.foo'], ['.long.foo']), handlerInfo('long-b', ['.long.foo'], ['.long.foo']), handlerInfo('short', ['.foo'])], owner: null },
        { scenario: 'a valid longest suffix', path: 'sample.long.foo', entries: [handlerInfo('short', ['.foo']), handlerInfo('long', ['.long.foo'])], owner: 'long' },
        { scenario: 'disabled duplicate ids', path: 'sample.long.foo', entries: [handlerInfo('duplicate', ['.long.foo'], [], true), handlerInfo('duplicate', ['.long.foo'], [], true), handlerInfo('short', ['.foo'])], owner: 'short' },
    ])('names the canonical handler for $scenario, with its description (P03)', async ({ path, entries, owner }) => {
        catalog = entries;
        await info([vf(path, 'bytes')]);
        const others = entries.filter(entry => entry.id !== owner);
        for (const other of others) expect(within(handlerSection()).queryByText(other.name)).toBeNull();
        if (owner === null) {
            // A clash is named as one: the shorter type's handler is not offered in its place.
            expect(within(handlerSection()).getByText('More than one handler claims ‘.long.foo’ files, so none is in force.')).toBeInTheDocument();
            expect(within(handlerSection()).queryByRole('button')).toBeNull();
            return;
        }
        const handler = entries.find(entry => entry.id === owner)!;
        const row = within(handlerSection()).getByRole('button');
        expect(within(row).getByText(handler.name)).toBeInTheDocument();
        expect(within(row).getByText(handler.description)).toBeInTheDocument();
        expect(within(row).queryByText('built-in')).toBeNull();
    });

    it('a built-in handler carries the built-in chip (P03)', async () => {
        catalog = [{ ...handlerInfo('renderer', ['.renderer']), builtin: true, cell_id: 'definitions' }];
        await info([vf('pkg/view.renderer', 'id: view\nname: View')]);
        const row = within(handlerSection()).getByRole('button');
        expect(within(row).getByText('renderer handler')).toBeInTheDocument();
        expect(within(row).getByText('built-in')).toBeInTheDocument();
        expect(within(row).getByText('What renderer checks')).toBeInTheDocument();
    });

    it('the handler\'s row closes the panel and asks Capabilities to show the handler\'s card (P03)', async () => {
        seatHandlers([handlerInfo('custom', ['.custom'])]);
        await info([vf('sample.custom', 'bytes')]);
        const row = within(handlerSection()).getByRole('button');
        expect(row).toHaveAttribute('title', 'Show the handler’s card in Capabilities');
        fireEvent.click(row);
        const state = useCollimateStore.getState();
        expect(state.pickSessions.cell).toBeUndefined();
        expect(state.capabilitiesManageRequest).toMatchObject({ kind: 'handler', handler: { cellId: 'custom-cell', folder: 'custom' } });
        expect(state.capabilitiesManageRequest?.handler?.step).toBeUndefined();
        expect(spies.open).not.toHaveBeenCalled();
        expect(spies.openWith).not.toHaveBeenCalled();
    });

    it('with no handler claiming the file a note says so, by its suffix or plainly for a file with none (P03)', async () => {
        catalog = [handlerInfo('custom', ['.custom'])];
        await info([vf('notes.md', 'notes')]);
        expect(within(handlerSection()).getByText('No handler claims ‘.md’ files.')).toBeInTheDocument();
        expect(within(handlerSection()).queryByRole('button')).toBeNull();
        cleanup();
        await info([vf('Makefile', 'all:')]);
        expect(within(handlerSection()).getByText('No handler claims this file.')).toBeInTheDocument();
    });

    it('says the handler catalog is loading, never that no handler claims the file (P03)', async () => {
        serve(path => (path.endsWith('/handlers') ? new Promise(() => {}) : undefined));
        await info([vf('sample.custom', 'bytes')]);
        expect(within(handlerSection()).getByText('Loading handlers…')).toBeInTheDocument();
        expect(within(handlerSection()).queryByText(/No handler claims/)).toBeNull();
    });

    it('reports a failed handler catalog load in the step, never as "no handler" (P03)', async () => {
        serve(path => (path.endsWith('/handlers') ? { ok: false, status: 500, json: async () => ({ detail: 'Catalog down' }) } : undefined));
        await info([vf('sample.custom', 'bytes')]);
        expect(within(handlerSection()).getByRole('alert').textContent).toMatch(/could not be loaded: .*Catalog down/);
        expect(within(handlerSection()).queryByText(/No handler claims/)).toBeNull();
    });
});

describe('FilesPanel: the options add a definition to Capabilities', () => {
    const capabilityEntry = () => entries().find(o => o.firstElementChild?.textContent?.startsWith('Add '))!;
    const toApproval = async (initial = [vf('pkg/view.renderer', 'id: view\nname: View')]) => {
        await mount({ initial, active: null });
        fireEvent.keyDown(search(), { key: 'Enter' });
        await act(async () => { fireEvent.click(capabilityEntry()); });
        await screen.findByRole('button', { name: 'Confirm' });
    };
    /** Put the work cell on Capabilities instead of the board. */
    const seatOnCapabilities = () => {
        const next = { ...channel, main_stacks: [{ ...channel.main_stacks[0], cells: [channel.main_stacks[1].cells[0]] }] };
        useCollimateStore.setState({ activeChannel: next, state: { active_channel_id: channel.id, channels: [next] } });
    };

    it('leads a definition package\'s options with Add to Capabilities, named after the definition; never a plain file\'s or an options file\'s (P03)', async () => {
        await mount({ initial: [vf('notes.txt', 'notes'), vf('pkg/view.renderer', 'id: view\nname: View'), vf('tool/tool.generator', 'id: tool-id'), vf('settings.options', '')], active: null });
        optionsOf('view.renderer');
        expect(screen.queryAllByRole('heading')).toEqual([]);
        expect(entryLabels()).toEqual(['Add ‘View’ to Capabilities…', 'Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
        expect(selectedEntry()).toBe('Add ‘View’ to Capabilities…');
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        optionsOf('tool.generator');
        expect(capabilityEntry()).toHaveTextContent('Add ‘tool-id’ to Capabilities…');
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        for (const plain of ['notes.txt', 'settings.options']) {
            optionsOf(plain);
            expect(entryLabels()).toEqual(['Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
            fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        }
    });

    it('offers nothing to add on a cell that is itself on Capabilities, nor on a built-in card (P03)', async () => {
        seatOnCapabilities();
        await mount({ initial: [vf('pkg/view.renderer', 'id: view\nname: View'), vf('chk/chk.handler', 'id: chk\nname: Check')], active: null });
        for (const file of ['view.renderer', 'chk.handler']) {
            optionsOf(file);
            expect(entryLabels()).toEqual(['Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
            fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        }
        cleanup();
        useCollimateStore.setState({ activeChannel: channel, state: { active_channel_id: channel.id, channels: [channel] } });
        await mount({ initial: [vf('pkg/view.renderer', 'id: view\nname: View')], active: null, builtin: true });
        optionsOf('view.renderer');
        expect(entryLabels()).toEqual(['Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
    });

    it('the approval step: targets, the hashes as mono text, the files; Confirm posts the exact hash and revision; Back returns to the options (P03)', async () => {
        await toApproval();
        expect(screen.getByText('Add ‘View’ to Capabilities')).toBeInTheDocument();
        const targets = screen.getByRole('group', { name: 'Target' });
        expect(within(targets).getByRole('button', { name: 'New cell' })).toHaveAttribute('aria-pressed', 'true');
        expect(screen.getByText(preview.content_hash)).toHaveClass('font-mono');
        expect(screen.getByText('No mounted preview recorded')).toBeInTheDocument();
        expect(screen.getByText('main.tsx', { selector: 'li' })).toHaveClass('font-mono');
        expect(screen.getByText('.demo')).toBeInTheDocument();
        // The build facts are the Package section's, never summary rows.
        expect(screen.queryByText('derived_hash')).toBeNull();
        expect(screen.queryByText('kept_from_target')).toBeNull();
        expect(screen.queryByText(/waits for your review/)).toBeNull();
        expect(screen.queryByRole('switch')).toBeNull();
        expect(requests.filter(item => item.url.endsWith('/packages/preview')).map(item => item.body)).toEqual([{
            source_cell_id: 'cell', definition_path: 'pkg/view.renderer', target_cell_id: null,
        }]);
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        expect(optionsList()).toHaveAccessibleName('‘view.renderer’');
        await act(async () => { fireEvent.click(capabilityEntry()); });
        await act(async () => { fireEvent.click(await screen.findByRole('button', { name: 'Confirm' })); });
        expect(requests.find(item => item.url.endsWith('/packages/add'))?.body).toEqual({
            source_cell_id: 'cell', definition_path: 'pkg/view.renderer', target_cell_id: null,
            expected_content_hash: preview.content_hash, expected_target_revision: 'shown-target', replace: false,
        });
        expect(await screen.findByRole('status')).toHaveTextContent('Added');
        // Only a handler's card has a step to show.
        expect(screen.queryByRole('button', { name: 'Show in Capabilities' })).toBeNull();
        expect(search()).toBeInTheDocument();
    });

    it('a replacement needs the Replace switch; a stale refusal shows and nothing reads as added (P03)', async () => {
        refusal = true;
        await toApproval();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Shipped/ })); });
        expect(screen.getByRole('button', { name: /Shipped/ })).toHaveAttribute('aria-pressed', 'true');
        const confirm = screen.getByRole('button', { name: 'Confirm' });
        expect(confirm).toBeDisabled();
        expect(screen.getByText('old')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('switch', { name: 'Replace' }));
        expect(confirm).toBeEnabled();
        await act(async () => { fireEvent.click(confirm); });
        expect(await screen.findByRole('alert')).toHaveTextContent('The package changed');
        expect(screen.queryByRole('status')).toBeNull();
        expect(requests.find(item => item.url.endsWith('/packages/add'))?.body).toMatchObject({ target_cell_id: 'shipped', replace: true });
    });

    it('preselects the one cell the package replaces, with Replace on (P03)', async () => {
        preview = { ...PREVIEW, targets: [PREVIEW.targets[0], { ...PREVIEW.targets[1], replaces: true }] };
        await toApproval();
        expect(requests.filter(item => item.url.endsWith('/packages/preview')).map(item => item.body.target_cell_id)).toEqual([null, 'shipped']);
        expect(within(screen.getByRole('group', { name: 'Target' })).getByRole('button', { name: /Shipped/ })).toHaveAttribute('aria-pressed', 'true');
        expect(screen.getByRole('switch', { name: 'Replace' })).toBeChecked();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm' })); });
        expect(requests.find(item => item.url.endsWith('/packages/add'))?.body).toMatchObject({ target_cell_id: 'shipped', replace: true });
    });

    it('two cells holding the package preselect neither (P03)', async () => {
        preview = { ...PREVIEW, targets: [PREVIEW.targets[0], { ...PREVIEW.targets[1], replaces: true }, { cell_id: 'other', name: 'Other', replaces: true }] };
        await toApproval();
        expect(requests.filter(item => item.url.endsWith('/packages/preview'))).toHaveLength(1);
        expect(within(screen.getByRole('group', { name: 'Target' })).getByRole('button', { name: 'New cell' })).toHaveAttribute('aria-pressed', 'true');
    });

    it('a handler\'s Package section names the build it carries and the skills a Replace keeps; done, Show in Capabilities opens its review (P03)', async () => {
        preview = {
            ...PREVIEW, kind: 'handler', id: 'chk', folder: 'chk', files: ['chk.handler', 'derived/verify.ts'],
            targets: [PREVIEW.targets[0], { ...PREVIEW.targets[1], replaces: true }],
            summary: { name: 'Check', derived_hash: 'd'.repeat(64), kept_from_target: ['skills/house-style/SKILL.md'] },
        };
        addMessage = 'Added. Review and accept its build from its card in Capabilities.';
        await toApproval([vf('chk/chk.handler', 'id: chk\nname: Check'), vf('chk/derived/verify.ts', 'export {}')]);
        const pkg = screen.getByRole('group', { name: 'Package' });
        expect(within(pkg).getByText(/waits for your review/)).toHaveTextContent(`Build ${'d'.repeat(64)}: waits for your review`);
        expect(within(pkg).getByText(/Kept from the cell/)).toHaveTextContent('Kept from the cell:skills/house-style/SKILL.md');
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm' })); });
        expect(await screen.findByRole('status')).toHaveTextContent('Added. Review and accept its build from its card in Capabilities.');
        fireEvent.click(screen.getByRole('button', { name: 'Show in Capabilities' }));
        const state = useCollimateStore.getState();
        expect(state.pickSessions.cell).toBeUndefined();
        // The replaced cell's folder is where the handler now lives.
        expect(state.capabilitiesManageRequest).toMatchObject({ kind: 'handler', handler: { cellId: 'new', folder: 'kept-folder', step: 'review' } });
    });

    it('the summary reads a list of entries by their names, never as [object Object] (P03)', async () => {
        preview = {
            ...PREVIEW, kind: 'handler', id: 'chk', folder: 'chk', files: ['chk.handler'],
            summary: {
                name: 'Check', types: [{ ext: '.note' }, { ext: '.md', surface: true, render_as: '.txt' }],
                permissions: { docker: 'none', internet_allow: [] }, derived_hash: null, kept_from_target: [],
            },
        };
        await toApproval([vf('chk/chk.handler', 'id: chk\nname: Check')]);
        const summaryValue = (key: string) => screen.getByText(key, { selector: 'dt' }).nextElementSibling?.textContent;
        expect(summaryValue('types')).toBe('.note, .md (surface: true, render_as: .txt)');
        expect(summaryValue('permissions')).toBe('docker: none; internet_allow: none');
        expect(document.body.textContent).not.toContain('[object Object]');
    });

    it('a handler added without a build shows its card, with no step (P03)', async () => {
        preview = { ...PREVIEW, kind: 'handler', id: 'chk', folder: 'chk', files: ['chk.handler'], summary: { name: 'Check', derived_hash: null, kept_from_target: [] } };
        addMessage = 'Added. Build it from its card in Capabilities.';
        await toApproval([vf('chk/chk.handler', 'id: chk\nname: Check')]);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm' })); });
        expect(await screen.findByRole('status')).toHaveTextContent('Added. Build it from its card in Capabilities.');
        fireEvent.click(screen.getByRole('button', { name: 'Show in Capabilities' }));
        const request = useCollimateStore.getState().capabilitiesManageRequest;
        expect(request).toMatchObject({ kind: 'handler', handler: { cellId: 'new', folder: 'chk' } });
        expect(request?.handler?.step).toBeUndefined();
    });

    it('shows the last mounted preview hash beside the package hash, and approval still sends the package hash (P03)', async () => {
        const event = (active: boolean) => new CustomEvent('collimate:renderer-preview', { detail: { cellId: 'cell', definitionPath: 'pkg/view.renderer', contentHash: 'b'.repeat(64), mountId: 'approval-preview', active } });
        act(() => { window.dispatchEvent(event(true)); });
        act(() => { window.dispatchEvent(event(false)); });
        await toApproval();
        expect(screen.getByText('b'.repeat(64))).toHaveClass('font-mono');
        expect(screen.getByText(/mounted preview differs/)).toBeInTheDocument();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm' })); });
        expect(requests.find(item => item.url.endsWith('/packages/add'))?.body.expected_content_hash).toBe(preview.content_hash);
    });
});

describe('FilesPanel: a built-in handler\'s regenerated skills', () => {
    const BUILTIN = { ...handlerInfo('notes', ['.notes']), name: 'Notes', builtin: true, cell_id: 'definitions', folder: 'notes' };
    const REGENERATED = [
        vf('notes/notes.handler', 'id: notes\nname: Notes'),
        vf('notes/derived/skills/notes-authoring/SKILL.md', '# Notes'),
        vf('notes/derived/skills/notes-authoring/references/style.md', 'style'),
        vf('notes/derived/verify.ts', 'export {}'),
        vf('brief.md', 'brief'),
    ];
    const skillsEntry = () => entries().find(o => o.firstElementChild?.textContent?.startsWith('Use these skills'))!;
    const serveOffer = (answer: () => unknown = () => ({ ok: true, json: async () => ({ handler: { ...BUILTIN, built_hash: 'regen', accepted: false } }) })) =>
        serve(path => (path.endsWith('/builtin/handlers/notes/skills') ? answer() : undefined));

    it('a handler with a built-in\'s id offers its skills to that built-in instead of an add; the step lists them and Use skills offers them (P03)', async () => {
        catalog = [BUILTIN];
        serveOffer();
        await mount({ initial: REGENERATED, active: null });
        optionsOf('notes.handler');
        expect(screen.queryAllByRole('heading')).toEqual([]);
        expect(entryLabels()).toEqual(['Use these skills for the built-in ‘Notes’ handler…', 'Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
        expect(skillsEntry()).toHaveTextContent('Use these skills for the built-in ‘Notes’ handler…');
        await act(async () => { fireEvent.click(skillsEntry()); });
        expect(screen.getByText('Use these skills for the built-in ‘Notes’ handler')).toBeInTheDocument();
        expect(within(screen.getByRole('group', { name: 'Skills' })).getAllByRole('listitem').map(li => li.textContent)).toEqual([
            'notes/derived/skills/notes-authoring/SKILL.md', 'notes/derived/skills/notes-authoring/references/style.md',
        ]);
        expect(requests.some(item => item.url.endsWith('/packages/preview'))).toBe(false);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Use skills' })); });
        expect(requests.find(item => item.url.endsWith('/builtin/handlers/notes/skills'))?.body).toEqual({ source_cell_id: 'cell', folder: 'notes' });
        expect(await screen.findByRole('status')).toHaveTextContent('Skills offered. Review and accept them from the built-in card in Capabilities.');
        fireEvent.click(screen.getByRole('button', { name: 'Show in Capabilities' }));
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ kind: 'handler', handler: { cellId: 'definitions', folder: 'notes', step: 'review' } });
        expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
    });

    it('is greyed with its reason when the package holds no regenerated skills (P03)', async () => {
        catalog = [BUILTIN];
        await mount({ initial: [vf('notes/notes.handler', 'id: notes\nname: Notes')], active: null });
        optionsOf('notes.handler');
        expect(skillsEntry()).toHaveAttribute('aria-disabled', 'true');
        expect(skillsEntry()).toHaveTextContent('No regenerated skills under notes/derived/skills');
        fireEvent.click(skillsEntry());
        expect(optionsList()).toBeInTheDocument();
    });

    it('a refused offer is the step\'s error and nothing reads as offered (P03)', async () => {
        catalog = [BUILTIN];
        serveOffer(() => ({ ok: false, status: 400, json: async () => ({ detail: 'Offer skills from the cell where they were written' }) }));
        await mount({ initial: REGENERATED, active: null });
        optionsOf('notes.handler');
        await act(async () => { fireEvent.click(skillsEntry()); });
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Use skills' })); });
        expect(await screen.findByRole('alert')).toHaveTextContent('Offer skills from the cell where they were written');
        expect(screen.queryByRole('status')).toBeNull();
    });

    it('on a cell on Capabilities a built-in-id handler offers nothing (P03)', async () => {
        catalog = [BUILTIN];
        const next = { ...channel, main_stacks: [{ ...channel.main_stacks[0], cells: [channel.main_stacks[1].cells[0]] }] };
        useCollimateStore.setState({ activeChannel: next, state: { active_channel_id: channel.id, channels: [next] } });
        await mount({ initial: REGENERATED, active: null });
        optionsOf('notes.handler');
        expect(entryLabels()).toEqual(['Open', 'Open with…', 'Info', 'Duplicate…', 'Rename…', 'Delete…']);
    });
});

describe('FilesPanel: the openers', () => {
    it('the files rail\'s Find file opens the Files panel session, leaving history first (P03)', () => {
        const onExit = vi.fn(() => useCollimateStore.setState({ cellHistory: {} }));
        useCollimateStore.setState({ cellHistory: { cell: { nodeId: null, versionId: null } as never } });
        const layout = setFilesPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }), { collapsed: false });
        render(<FilesPane cell={{ id: 'cell', name: 'Work', files: FILES }} layout={layout} onLayoutChange={vi.fn()} onOpenFile={vi.fn()}
            onUpdateFiles={vi.fn()} readOnly={false} activePath={null}
            history={{ nodes: [], selNodeId: null, selVersionId: null, nested: false, onSelect: vi.fn(), onExit,
                sections: { node: null, loading: false, error: null, files: [], baseFiles: {}, onToggleNested: vi.fn(), sectionsOpen: { config: true, files: true, logs: true }, onToggleSection: vi.fn(), onOpenEntry: vi.fn(), activeEntry: null } }} />);
        const find = screen.getByRole('button', { name: 'Find file' });
        expect(find).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(find);
        expect(onExit).toHaveBeenCalledTimes(1);
        expect(useCollimateStore.getState().pickSessions.cell).toMatchObject({ kind: 'files', query: '' });
        expect(screen.queryByRole('combobox')).toBeNull();
    });

    it('Mod+P opens the Files panel on the selected cell, or the cell whose panel has the keys, leaving history first; never in the overview (P03)', () => {
        const key = () => new KeyboardEvent('keydown', { key: 'p', ctrlKey: true, cancelable: true });
        expect(is(key(), SHORTCUT_IDS.GLOBAL_FIND_FILE) || is(new KeyboardEvent('keydown', { key: 'p', metaKey: true }), SHORTCUT_IDS.GLOBAL_FIND_FILE)).toBe(true);
        expect(buildForwardedCombos().some(combo => combo.key.toLowerCase() === 'p' && (combo.ctrlKey || combo.metaKey))).toBe(true);
        const zoomed = key();
        executeFindFile(zoomed, { selectedCellId: 'cell', isZoomedOut: true });
        expect(zoomed.defaultPrevented).toBe(true);
        expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        executeFindFile(key(), { selectedCellId: 'cell', isZoomedOut: false });
        expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
        // From inside another cell's runners panel: that cell gets the files.
        act(() => useCollimateStore.getState().openPickSession('other', 'runners'));
        const runners = document.createElement('div');
        runners.setAttribute('data-cell-panel', 'runners');
        runners.setAttribute('data-pick-cell', 'other');
        runners.setAttribute(KEYBOARD_OWNER_ATTR, '');
        const input = document.createElement('input');
        runners.appendChild(input);
        document.body.appendChild(runners);
        try {
            input.addEventListener('keydown', e => executeFindFile(e, { selectedCellId: 'cell', isZoomedOut: false }));
            input.dispatchEvent(key());
            expect(useCollimateStore.getState().pickSessions.other?.kind).toBe('files');
        } finally { runners.remove(); }
        // A cell in version history leaves it first, as the rail's Find file does.
        act(() => useCollimateStore.getState().closePickSession('other', 'files'));
        useCollimateStore.setState({ cellHistory: { other: { nodeId: null, versionId: null } as never } });
        executeFindFile(key(), { selectedCellId: 'other', isZoomedOut: false });
        expect(useCollimateStore.getState().cellHistory.other).toBeUndefined();
        expect(useCollimateStore.getState().pickSessions.other?.kind).toBe('files');
        // Another keyboard owner (a menu) keeps the key.
        act(() => useCollimateStore.getState().closePickSession('cell', 'files'));
        const menu = document.createElement('div');
        menu.setAttribute(KEYBOARD_OWNER_ATTR, '');
        document.body.appendChild(menu);
        try {
            menu.addEventListener('keydown', e => executeFindFile(e, { selectedCellId: 'cell', isZoomedOut: false }));
            menu.dispatchEvent(key());
            expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
        } finally { menu.remove(); }
    });
});
