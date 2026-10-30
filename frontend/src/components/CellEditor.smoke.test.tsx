import 'fake-indexeddb/auto';
import { fixtureBodyHash } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { FocusProvider, useFocus } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, VirtualFile } from '@/lib/types';

// The guest pool: a recorder for the reclaim of a vanished `.webview`'s
// guest and of its wakes; every other member is the context's inert default.
const pool = vi.hoisted(() => ({ destroyGuestsWhere: vi.fn(), invalidate: vi.fn() }));
vi.mock('@/contexts/GuestPoolContext', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/contexts/GuestPoolContext')>();
    return {
        ...actual,
        useGuestPool: () => ({
            acquire: () => {}, acquireGuest: () => null, release: () => {}, destroyGuest: () => {},
            destroyGuestsWhere: pool.destroyGuestsWhere, containerOf: () => null,
            setPointerEvents: () => {}, getContentWindow: () => null,
            getElement: () => null, getGuestId: () => null, setGuestReady: () => {},
            setGuestInteractive: () => {}, setGuestConcealed: () => {}, setGuestCrashed: () => {},
            refreshGuestLayout: () => {}, broadcastToCell: () => {},
            registerOverlay: () => {}, invalidate: pool.invalidate, markMoving: () => {},
            registerMotionLayer: () => () => {}, waitUntilReady: () => Promise.resolve(),
            setVisible: () => {}, bringCellsToFront: () => {}, onMessage: () => () => {},
        }),
    };
});
// The conceal around the content pane: records each `open` it is given,
// then provides as the real one does.
const conceal = vi.hoisted(() => ({ open: [] as boolean[] }));
vi.mock('./cellPanel/CellPanel', async (importOriginal) => {
    const actual = await importOriginal<typeof import('./cellPanel/CellPanel')>();
    return {
        ...actual,
        CellPanelConceal: (props: Parameters<typeof actual.CellPanelConceal>[0]) => {
            conceal.open.push(props.open);
            return <actual.CellPanelConceal {...props} />;
        },
    };
});
vi.mock('@/stores/useRunnerStore',()=>({useRunnerCatalog:()=>({data:[],error:null,busy:false,refresh:async()=>{}})}));
vi.mock('./runner/RunnerPicker', () => ({ RunnerPicker: () => <div data-testid="runner-picker" /> }));
vi.mock('./cellPanel/FilesPanel', () => ({ FilesPanel: () => <div data-testid="files-panel" /> }));
const flushers = vi.hoisted(() => ({ register: vi.fn((_cellId: string, _fn: () => void) => () => {}) }));
vi.mock('@/lib/cellSaveBarrier', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    registerCellSaveBarrier: flushers.register,
}));
vi.mock('@/hooks/useZoomTransition', () => ({ useIsZoomTransitioning: () => false }));
vi.mock('@/hooks/useRendererId', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    useSupportedExtensions: () => [{ ext: '.txt', renderers: ['Text'] }],
}));
vi.mock('@/hooks/useIdleCheckpoint', () => ({
    useIdleCheckpoint: () => ({ notifyEdit: vi.fn(), flushCheckpoint: vi.fn() }),
}));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    listCellVersions: vi.fn().mockResolvedValue({ versions: [] }),
    fetchCellHistory: vi.fn().mockResolvedValue({ nodes: [], total_versions: 1, current: { node_id: null, version_cell_id: 'cell-1' } }),
}));

vi.mock('@/lib/api/client',async original=>({...await original<Record<string,unknown>>(),requestJson:vi.fn(async (_path,_method,operation)=> {
    if(!operation?.mutations)return {conflicts:[],verdicts:{},pending:false};
    return {operation_id:operation.operation_id,outcome:'accepted',conflicts:[],files:operation.mutations.map((mutation:import('@/lib/fileProtocol').FileMutation)=>({
        path:mutation.path,file_revision:mutation.base_revision+1,body_hash:mutation.kind==='delete'?null:mutation.blob_hash??fixtureBodyHash(mutation.content_text??''),
        encoding:mutation.encoding??'utf-8',language:mutation.language??'text',writer_id:mutation.writer_id,edit_seq:mutation.edit_seq,deleted:mutation.kind==='delete',
    }))};
})}));
import CellEditor from './CellEditor';
import { allLeaves, allTabs, createDefaultLayout, findFileTab, openCompareTab, openFileTab, openRunsPane, setActiveLeaf, setFilesPane, splitWithTab } from '@/lib/cellLayout';
import { GUEST_FOCUS_EVENT } from './guestMount';
import { isCellOccluded } from '@/lib/guestOcclusion';

const file = (name: string, content = 'x'): VirtualFile => (fileFixture(name, content, {language: 'plaintext'}));
const cell = (extra: Partial<Cell> = {}): Cell => ({
    id: 'cell-1', name: 'Test cell', files: [file('a.txt')], ...extra,
});

function seed(c: Cell) {
    const channel={id:'ch-id',name:'ch',nav_stacks:[],nav_selection_id:null,main_selection_id:'s',main_stacks:[{id:'s',name:'S',cells:[c]}]};
    useCollimateStore.setState({state:{active_channel_id:'ch-id',channels:[channel]},activeChannel:channel,cellCache:{[c.id]:c}});
}
const mount = (c: Cell, props: Record<string, unknown> = {}) => {
    seed(c);
    return render(<FocusProvider><div style={{width:600,height:400}}><CellEditor cell={c} onUpdate={vi.fn()} {...props}/></div></FocusProvider>);
};

function EditableCell({ initial, onUpdate }: { initial: Cell; onUpdate: (cell: Cell) => void }) {
    const [current, setCurrent] = useState(()=>{seed(initial);return initial;});
    return <FocusProvider><CellEditor cell={current} onUpdate={next => { setCurrent(next); onUpdate(next); }} /></FocusProvider>;
}

describe('CellEditor — layout integration smoke', () => {
    beforeEach(() => {
        useCollimateStore.setState({
            cellLayouts: {}, cellCache: {}, tabDrag: null, dockDrag: null,
            activeChannel: { id: 'ch-id', name: 'ch', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null },
            runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        });
        vi.clearAllMocks();
        seed(cell());
        // Native embedding may request transport URLs in jsdom.
        (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
        (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
    });

    it('starts an untouched empty tab with the Files pane hidden; the tab bar\'s toggle shows it on the left (C12)', async () => {
        await act(async () => { mount(cell()); });
        expect(screen.getByRole('tablist')).toBeTruthy();
        expect(screen.getByText('New tab')).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Open existing or create a new file' })).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Folder' })).toBeNull();
        await act(async () => { screen.getByRole('button', { name: 'Show files' }).click(); });
        // Shown, the rail carries the views and the toggle leaves the tab bar.
        expect(screen.getByRole('button', { name: 'Folder' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Tree' })).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Show files' })).toBeNull();
        const pane = screen.getByRole('complementary', { name: 'Files' });
        expect(pane.style.width).toBe('194px');
        // Left of the tab/split view, inside the cell's width.
        expect(pane.compareDocumentPosition(screen.getByRole('tablist')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(useCollimateStore.getState().cellLayouts['cell-1'].filesPane.collapsed).toBe(false);
        await act(async () => { screen.getByRole('button', { name: 'Hide files' }).click(); });
        expect(screen.queryByRole('complementary', { name: 'Files' })).toBeNull();
        expect(screen.getByRole('button', { name: 'Show files' })).toBeTruthy();
    });

    it('the pane row runs per dock side with the pane first (FP-02)', async () => {
        const layout = createDefaultLayout({ filePaths: ['a.txt'], pinnedPath: null });
        layout.filesPane = { ...layout.filesPane, collapsed: false };
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout } });
        let view!: ReturnType<typeof mount>;
        await act(async () => { view = mount(cell()); });
        const row = view.container.querySelector('[data-files-dock-region]') as HTMLElement;
        const content = () => row.lastElementChild as HTMLElement;
        const before = content();
        const expected = { left: 'flex-row', top: 'flex-col' } as const;
        for (const side of ['left', 'top', 'left'] as const) {
            await act(async () => { useCollimateStore.getState().updateCellLayout('cell-1', l => setFilesPane(l, { side })); });
            expect(row.className.split(/\s+/)).toContain(expected[side]);
            expect(row.firstElementChild).toBe(screen.getByRole('complementary', { name: 'Files' }));
        }
        // The content pane is the same element throughout: no remount.
        expect(content()).toBe(before);
    });

    it('a dock drag over the cell draws the band on its edge and occludes the cell\'s guests; none otherwise (FP-04)', async () => {
        let view!: ReturnType<typeof mount>;
        await act(async () => { view = mount(cell()); });
        const band = () => view.container.querySelector('[data-dock-preview]') as HTMLElement | null;
        expect(band()).toBeNull();
        expect(isCellOccluded('cell-1')).toBe(false);
        await act(async () => { useCollimateStore.getState().setDockDrag({ cellId: 'cell-1', pane: 'files', side: null }); });
        expect(band()).toBeNull();
        // The band is cell DOM: the cell's native guests freeze for the drag.
        expect(isCellOccluded('cell-1')).toBe(true);
        await act(async () => { useCollimateStore.getState().setDockDrag({ cellId: 'cell-1', pane: 'files', side: 'left' }); });
        expect(band()).toHaveAttribute('data-dock-preview', 'left');
        expect(band()!.textContent).toBe('Dock files on the left');
        expect(band()!.className).toContain('pointer-events-none');
        expect(band()!.className).toContain('border-dashed');
        expect(band()!.className.split(' ')).toEqual(expect.arrayContaining(['left-1', 'top-1', 'bottom-1']));
        expect(band()!.style.width).toBe('194px');
        expect(band()!.closest('[data-pane-region]')).not.toBeNull();
        await act(async () => { useCollimateStore.getState().setDockDrag({ cellId: 'cell-1', pane: 'files', side: 'top' }); });
        expect(band()).toHaveAttribute('data-dock-preview', 'top');
        expect(band()!.textContent).toBe('Dock files on the top');
        expect(band()!.className.split(' ')).toEqual(expect.arrayContaining(['top-1', 'left-1', 'right-1']));
        expect(band()!.style.height).toBe('198px');
        await act(async () => { useCollimateStore.getState().setDockDrag({ cellId: 'other', pane: 'files', side: 'left' }); });
        expect(band()).toBeNull();
        expect(isCellOccluded('cell-1')).toBe(false);
        await act(async () => { useCollimateStore.getState().setDockDrag(null); });
    });

    it('retains a persisted open sidebar and its selected folder', async () => {
        const layout = createDefaultLayout({ filePaths: ['sub/a.txt'], pinnedPath: null });
        layout.filesPane = { ...layout.filesPane, collapsed: false, currentFolder: 'sub' };
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout } });
        await act(async () => { mount(cell({ files: [file('sub/a.txt')] })); });
        expect(screen.getByRole('button', { name: 'Folder' })).toBeTruthy();
        expect(useCollimateStore.getState().cellLayouts['cell-1'].filesPane).toMatchObject({ collapsed: false, currentFolder: 'sub' });
    });

    it('the empty tab\'s button opens the cell\'s Files panel', async () => {
        await act(async () => { mount(cell()); });
        expect(screen.queryByTestId('files-panel')).toBeNull();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open existing or create a new file' })); });
        expect(useCollimateStore.getState().pickSessions['cell-1']?.kind).toBe('files');
        expect(screen.getByTestId('files-panel')).toBeTruthy();
    });

    it('a read-only cell\'s empty tab offers no panel', async () => {
        await act(async () => { mount(cell(), { readOnly: true }); });
        expect(screen.getByRole('button', { name: 'Open existing or create a new file' })).toBeDisabled();
    });

    it('seeds a pinned file tab and reconciles a vanished file away', async () => {
        const c = cell({ pinned: 'a.txt' });
        const { rerender } = await act(async () => mount(c));
        expect(screen.getByRole('tab', { name: /a\.txt/ })).toBeTruthy();
        let layout = useCollimateStore.getState().cellLayouts['cell-1'];
        expect(layout.root.type).toBe('leaf');
        expect(layout.root.type === 'leaf' && layout.root.tabs.length).toBe(1);

        await act(async () => {
            rerender(
                <FocusProvider>
                    <div style={{ width: 600, height: 400 }}>
                        <CellEditor cell={{ ...c, files: [], pinned: undefined }} onUpdate={vi.fn()} />
                    </div>
                </FocusProvider>,
            );
        });
        layout = useCollimateStore.getState().cellLayouts['cell-1'];
        expect(layout.root.type === 'leaf' && layout.root.tabs.map(t => t.kind)).toEqual(['empty']);
    });

    it('a structural edit preserves an untouched large accepted descriptor', async () => {
        const onUpdate = vi.fn();
        const big: VirtualFile = fileFixture('big.md', 'large body '.repeat(20000), {language: 'markdown'});
        await act(async () => { render(<EditableCell initial={cell({ files: [file('sub/a.txt'), big] })} onUpdate={onUpdate} />); });
        // The Files pane's tree: a new file in the `sub` folder.
        await act(async () => { screen.getByRole('button', { name: 'Show files' }).click(); });
        fireEvent.click(document.querySelector('[data-filenode-path="sub"] [title="New file in this folder"]')!);
        fireEvent.change(screen.getByPlaceholderText('filename'), { target: { value: 'created.txt' } });
        fireEvent.click(screen.getByRole('button', { name: 'Create' }));
        await waitFor(()=>expect(useCollimateStore.getState().cellCache['cell-1'].files.some(f=>f.name==='sub/created.txt')).toBe(true));
        expect(useCollimateStore.getState().cellCache['cell-1'].files.find(f=>f.name==='big.md')).toEqual(big);
    });

    it('releases the file-editor focus mode when the owning editor unmounts', async () => {
        const layout = openFileTab(createDefaultLayout({ filePaths: ['a.txt'], pinnedPath: null }), 'a.txt', null);
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout } });
        const Probe = () => <span data-testid="mode">{useFocus().mainFocusMode}</span>;
        const tree = (show: boolean) => (
            <FocusProvider>
                <Probe />
                {show && <CellEditor cell={cell()} onUpdate={vi.fn()} isActive />}
            </FocusProvider>
        );
        const { rerender } = await act(async () => render(tree(true)));
        expect(screen.getByTestId('mode')).toHaveTextContent('file-editor');
        await act(async () => { rerender(tree(false)); });
        expect(screen.getByTestId('mode')).toHaveTextContent('window');
    });

    it('a focused compare tab claims the file-editor focus mode as a file tab does; Esc closes it and hands the mode back (R12)', async () => {
        const files = [file('a.txt', 'one\n'), file('b.txt', 'two\n')];
        useCollimateStore.setState({ state: { channels: [{
            id: 'ch-id', name: 'ch', main_selection_id: 's', nav_selection_id: null, nav_stacks: [],
            main_stacks: [{ id: 's', name: 'Work', cells: [cell({ files })] }],
        }], active_channel_id: 'ch-id' } } as never);
        const side = (fileName: string) => ({ channel: 'ch', cellId: 'cell-1', fileName, label: 'Work / Test cell' });
        const layout = openCompareTab(createDefaultLayout({ filePaths: ['a.txt', 'b.txt'], pinnedPath: null }),
            { base: side('b.txt'), current: side('a.txt') });
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout } });
        const Probe = () => <span data-testid="mode">{useFocus().mainFocusMode}</span>;
        try {
            await act(async () => { render(
                <FocusProvider>
                    <Probe />
                    <CellEditor cell={cell({ files })} onUpdate={vi.fn()} isActive />
                </FocusProvider>,
            ); });
            expect(screen.getByTestId('mode')).toHaveTextContent('file-editor');
            await act(async () => { fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' }); });
            await waitFor(() => expect(allTabs(useCollimateStore.getState().cellLayouts['cell-1']).map(t => t.kind)).toEqual(['empty']));
            expect(screen.getByTestId('mode')).toHaveTextContent('window');
        } finally {
            useCollimateStore.setState({ state: null } as never);
        }
    });
});

describe('CellEditor — runs in the cell', () => {
    // A synthetic run; every file under its folder is its lock.
    const run = (key: 'a' | 'b') => ({
        runId: `run-${key}`, cellId: 'cell-1', channel: 'ch', runnerId: `r${key}`, runnerName: `Runner ${key.toUpperCase()}`,
        generatorName: 'g', label: `r${key}-${key.repeat(8)}`, folder: `_runs/r${key}-${key.repeat(8)}`,
    });
    const RUN_A = run('a');
    const A_FILE = `${RUN_A.folder}/out.txt`;
    const liveRuns = (...runs: ReturnType<typeof run>[]) => useCollimateStore.setState({
        runs: Object.fromEntries(runs.map(r => [r.runId, r])),
    });
    const withTabs = (files: string[], ...open: string[]) => {
        let layout = createDefaultLayout({ filePaths: files, pinnedPath: null });
        for (const path of open) layout = openFileTab(layout, path, null, undefined, { newTab: true });
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout } });
    };

    beforeEach(() => {
        useCollimateStore.setState({
            cellLayouts: {}, cellCache: {}, tabDrag: null,
            activeChannel: { id: 'ch-id', name: 'ch', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null },
            runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {}, cellLogRuns: {},
        });
        vi.clearAllMocks();
        (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
        (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
    });

    it('a run\'s file tab reads "<basename> · <runner> <short id>" and its renderer host names the file', async () => {
        liveRuns(RUN_A);
        withTabs(['a.txt', A_FILE], A_FILE);
        await act(async () => { mount(cell({ files: [file('a.txt'), file(A_FILE)] }), { isActive: true }); });
        const tab = screen.getByRole('tab', { name: /out\.txt/ });
        expect(within(tab).getByTestId('tab-run-chip').textContent).toContain('Runner A aaaaaaaa');
        expect(tab).toHaveAccessibleName(/out\.txt\s*·\s*Runner A aaaaaaaa/);
        expect(document.querySelector(`[data-file-path="${A_FILE}"]`)).not.toBeNull();
        // An ordinary, closable tab.
        expect(screen.getByLabelText(/^Close out\.txt/)).toBeTruthy();
    });

    it('the content region is the pane region alone, reaching the cell\'s bottom: no bar, no inset declared; the log is in the status pane inside it', async () => {
        const layout = openRunsPane(createDefaultLayout({ filePaths: ['a.txt'], pinnedPath: null }));
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout }, runsPaneView: { 'cell-1': { view: 'all', option: 'log' } } });
        await act(async () => { mount(cell()); });
        const region = document.querySelector('[data-content-region]') as HTMLElement;
        const panes = region.firstElementChild as HTMLElement;
        expect(panes).toHaveAttribute('data-pane-region');
        expect([...region.children]).toEqual([panes]);
        expect(panes.className).toMatch(/\bflex-1\b/);
        expect(region.getAttribute('style')).toBeNull();
        expect(panes.getAttribute('style')).toBeNull();
        const log = screen.getByTestId('run-log');
        const docked = panes.querySelector('[data-runs-dock-region]') as HTMLElement;
        expect(docked.lastElementChild).toHaveAttribute('data-runs-pane');
        expect(docked.lastElementChild!.contains(log)).toBe(true);
    });

    it('the runners panel opens while runs are live on the cell (P02)', async () => {
        liveRuns(RUN_A);
        useCollimateStore.setState({ pickSessions: { 'cell-1': { kind: 'runners', query: '', at: 1 } } });
        await act(async () => { mount(cell({ files: [file('a.txt'), file(A_FILE)] })); });
        expect(screen.getByTestId('runner-picker')).toBeTruthy();
    });

    it('mounts one panel per cell, the one its session names, over an isolated pane row (P01)', async () => {
        const kinds = [['runners', 'runner-picker'], ['files', 'files-panel']] as const;
        for (const [kind, testId] of kinds) {
            useCollimateStore.setState({ pickSessions: { 'cell-1': { kind, query: '', at: 1 } } });
            const view = await act(async () => mount(cell()));
            const panel = screen.getByTestId(testId);
            for (const [, other] of kinds) if (other !== testId) expect(screen.queryByTestId(other)).toBeNull();
            // The panel sits over the panes, whose row is its own stacking
            // context, in a slot reaching the region's bottom: its scrim
            // runs full height, its sheet ends 12px above it (CellPanel).
            const slot = panel.closest('[data-cell-panel-slot]')!;
            expect(slot).not.toBeNull();
            expect(slot.className).toMatch(/\binset-0\b/);
            expect(slot.parentElement!).toHaveAttribute('data-pane-region');
            expect(slot.previousElementSibling!.className).toMatch(/\bisolate\b/);
            view.unmount();
        }
    });

    it('a Capabilities cell never shows the runners panel, yet shows its other panels (P01)', async () => {
        useCollimateStore.setState({ pickSessions: { 'cell-1': { kind: 'runners', query: '', at: 1 } } });
        let view = await act(async () => mount(cell(), { capabilitiesCell: true }));
        expect(screen.queryByTestId('runner-picker')).toBeNull();
        view.unmount();
        useCollimateStore.setState({ pickSessions: { 'cell-1': { kind: 'files', query: '', at: 1 } } });
        view = await act(async () => mount(cell(), { capabilitiesCell: true }));
        expect(screen.getByTestId('files-panel')).toBeTruthy();
        view.unmount();
        useCollimateStore.setState({ pickSessions: { 'cell-1': { kind: 'files', query: '', at: 1 } } });
        await act(async () => { mount(cell(), { readOnly: true }); });
        expect(screen.queryByTestId('files-panel')).toBeNull();
    });

    it('a Capabilities cell mounts no status pane: a persisted open pane is not rendered, and a status dock drag draws no band', async () => {
        const layout = openRunsPane(createDefaultLayout({ filePaths: ['a.txt'], pinnedPath: null }));
        useCollimateStore.setState({ cellLayouts: { 'cell-1': layout }, runsPaneView: { 'cell-1': { view: 'status' } } });
        const view = await act(async () => mount(cell(), { capabilitiesCell: true }));
        expect(view.container.querySelector('[data-runs-pane]')).toBeNull();
        expect(screen.queryByTestId('status-view')).toBeNull();
        // The persisted layout is left as it was: nothing rewrites it.
        expect(useCollimateStore.getState().cellLayouts['cell-1'].runsPane.open).toBe(true);
        await act(async () => { useCollimateStore.getState().setDockDrag({ cellId: 'cell-1', pane: 'runs', side: 'bottom' }); });
        expect(view.container.querySelector('[data-dock-preview]')).toBeNull();
        await act(async () => { useCollimateStore.getState().setDockDrag(null); });
        view.unmount();
        // The same layout on any other cell shows its status pane.
        await act(async () => { mount(cell()); });
        expect(screen.getByTestId('status-view')).toBeTruthy();
    });

    it('the full-window view of a Capabilities cell shows no status caret; any other cell\'s does', async () => {
        for (const capabilitiesCell of [true, false]) {
            const view = await act(async () => mount(cell(), { capabilitiesCell }));
            await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Expand the cell view to the full window' })); });
            expect(screen.getByRole('button', { name: 'Back to the canvas' })).toBeTruthy();
            expect(!!screen.queryByTestId('runs-pane-toggle-strip')).toBe(!capabilitiesCell);
            view.unmount();
        }
    });

    it('registers its editors\' pending autosave for a run start', async () => {
        await act(async () => { mount(cell()); });
        expect(flushers.register).toHaveBeenCalledWith('cell-1', expect.any(Function));
    });

    it('a vanished `.webview` file reclaims its native guest', async () => {
        const tab = `${RUN_A.folder}/tab-1.webview`;
        const view = await act(async () => mount(cell({ files: [file('a.txt'), file(tab)] })));
        expect(pool.destroyGuestsWhere).not.toHaveBeenCalled();
        await act(async () => {
            view.rerender(
                <FocusProvider>
                    <div style={{ width: 600, height: 400 }}>
                        <CellEditor cell={cell({ files: [file('a.txt')] })} onUpdate={vi.fn()} />
                    </div>
                </FocusProvider>,
            );
        });
        expect(pool.destroyGuestsWhere).toHaveBeenCalledTimes(1);
        const match = pool.destroyGuestsWhere.mock.calls[0][0] as (id: string) => boolean;
        expect(match(`guest::cell-1::web::${tab}::0`)).toBe(true);
        expect(match(`guest::cell-1::web::other.webview::0`)).toBe(false);
        expect(match(`guest::cell-2::web::${tab}::0`)).toBe(false);
    });
});

describe('CellEditor — split panes follow focus the DOM cannot see', () => {
    const files = [file('a.txt'), file('b.txt')];
    // a.txt in the left leaf (active), b.txt split off to the right.
    const seedSplit = () => {
        let l = createDefaultLayout({ filePaths: files.map(f => f.name), pinnedPath: null });
        l = openFileTab(l, 'a.txt', null);
        l = openFileTab(l, 'b.txt', null, undefined, { newTab: true });
        const b = findFileTab(l, 'b.txt')!;
        l = splitWithTab(l, b.leaf.id, b.tab.id, 'right');
        const left = allLeaves(l.root).find(leaf => leaf.tabs.some(t => t.kind === 'file' && t.path === 'a.txt'))!;
        l = setActiveLeaf(l, left.id);
        useCollimateStore.setState({ cellLayouts: { 'cell-1': l } });
        return allLeaves(l.root).find(leaf => leaf.id !== left.id)!.id;
    };

    beforeEach(() => {
        useCollimateStore.setState({
            cellLayouts: {}, cellCache: {}, tabDrag: null,
            activeChannel: { id: 'ch-id', name: 'ch', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null },
            runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        });
        (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
        (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
    });

    it('a native guest taking focus makes its leaf the active one', async () => {
        const right = seedSplit();
        await act(async () => { mount(cell({ files })); });
        const host = document.querySelector('[data-file-path="b.txt"]') as HTMLElement;
        await act(async () => { host.dispatchEvent(new CustomEvent(GUEST_FOCUS_EVENT, { bubbles: true })); });
        expect(useCollimateStore.getState().cellLayouts['cell-1'].activeLeafId).toBe(right);
    });

    it('a vertical wheel over a leaf makes it the active one; a sideways or pinch wheel does not', async () => {
        const right = seedSplit();
        await act(async () => { mount(cell({ files })); });
        const host = document.querySelector('[data-file-path="b.txt"]') as HTMLElement;
        const wheel = (init: WheelEventInit) => act(async () => { host.dispatchEvent(new WheelEvent('wheel', { bubbles: true, ...init })); });
        const active = () => useCollimateStore.getState().cellLayouts['cell-1'].activeLeafId;
        const left = active();
        await wheel({ deltaX: 60, deltaY: 10 });
        await wheel({ deltaY: 30, ctrlKey: true });
        expect(active()).toBe(left);
        await wheel({ deltaY: 30 });
        expect(active()).toBe(right);
    });

    it('an iframe taking focus (the window blurs into it) makes its leaf the active one', async () => {
        const right = seedSplit();
        await act(async () => { mount(cell({ files })); });
        const host = document.querySelector('[data-file-path="b.txt"]') as HTMLElement;
        const iframe = document.createElement('iframe');
        host.appendChild(iframe);
        const spy = vi.spyOn(document, 'activeElement', 'get').mockReturnValue(iframe);
        try {
            await act(async () => { window.dispatchEvent(new Event('blur')); });
        } finally {
            spy.mockRestore();
        }
        expect(useCollimateStore.getState().cellLayouts['cell-1'].activeLeafId).toBe(right);
    });
});

describe('CellEditor — the full-window view under the Commands pane', () => {
    beforeEach(() => {
        useCollimateStore.setState({
            cellLayouts: {}, cellCache: {}, tabDrag: null,
            activeChannel: { id: 'ch-id', name: 'ch', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null },
            runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
            commandPane: { open: false, side: 'left', query: '', recents: [], view: { kind: 'commands' } }, maximizedOverlayCellId: null,
        });
        (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
        (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
    });
    const expand = async () => {
        await act(async () => { screen.getByRole('button', { name: 'Expand the cell view to the full window' }).click(); });
        const view = document.querySelector<HTMLElement>('[data-full-window]')!;
        expect(view.parentElement).toBe(document.body);
        expect(useCollimateStore.getState().maximizedOverlayCellId).toBe('cell-1');
        return view;
    };
    const pane = (open: boolean, side: 'left' | 'right' = 'left') => act(() => {
        useCollimateStore.setState({ commandPane: { open, side, query: '', recents: [], view: { kind: 'commands' } } });
    });

    it('the region insets by the open pane\'s width on its side, sliding with it, and the view keeps its whole width (polish)', async () => {
        await act(async () => { mount(cell()); });
        const view = await expand();
        expect(view.querySelector('[data-content-region]')).not.toBeNull();
        expect(view.className).toMatch(/\bfixed\b.*\binset-0\b.*\bz-\[450\]/);
        expect(view.style.paddingLeft).toBe('0px');
        expect(view.style.paddingRight).toBe('0px');
        expect(view.style.transition).toContain('padding-left 160ms ease');
        expect(view.style.transition).toContain('padding-right 160ms ease');
        pane(true, 'left');
        expect(view.style.paddingLeft).toBe('352px');
        expect(view.style.paddingRight).toBe('0px');
        pane(true, 'right');
        expect(view.style.paddingLeft).toBe('0px');
        expect(view.style.paddingRight).toBe('352px');
        pane(false, 'right');
        expect(view.style.paddingRight).toBe('0px');
    });

    it('the strip\'s line runs the window\'s width under the region, so it goes on under the pane (polish)', async () => {
        await act(async () => { mount(cell()); });
        const full = await expand();
        pane(true, 'left');
        const ground = full.querySelector<HTMLElement>('[data-full-window-ground]')!;
        expect(ground).toHaveAttribute('aria-hidden');
        // Behind the region: first in the view, the region after it.
        expect(full.firstElementChild).toBe(ground);
        expect(ground.nextElementSibling).toHaveAttribute('data-content-region');
        expect(ground.className).toMatch(/\babsolute\b.*\binset-0\b/);
        expect(ground.children).toHaveLength(1);
        const strip = ground.firstElementChild as HTMLElement;
        expect(strip.className).toMatch(/\binset-x-0\b.*\btop-0\b.*\bh-\[1\.875rem\].*\bborder-b\b.*\bborder-line\b/);
    });

    it('closes when the store lets go of it (a pane command that leaves the cell), flushing first (polish)', async () => {
        await act(async () => { mount(cell()); });
        await expand();
        await act(async () => { useCollimateStore.getState().setMaximizedOverlayCellId(null); });
        await waitFor(() => expect(document.querySelector('[data-full-window]')).toBeNull());
        expect(screen.getByRole('button', { name: 'Expand the cell view to the full window' })).toBeInTheDocument();
        expect(useCollimateStore.getState().maximizedOverlayCellId).toBeNull();
        // And expands again as before.
        await expand();
    });
});
