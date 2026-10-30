// The Commands pane hosting the selected cell's picker, end to end: the
// real pane, the real cell editor and the real cell panel (each kind
// reduced to its CellPanel), the scene keyboard and the run request.

import 'fake-indexeddb/auto';
import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import { FocusProvider, type FocusContextType } from '@/contexts/FocusContext';
import { CommandPane, type CommandPaneScene } from './CommandPane';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { useKeyboardHandler } from '@/hooks/useKeyboardHandler';
import { useRunRequest } from '@/hooks/useRunRequest';
import { openFilesPanel } from '@/hooks/sceneKeyboardEffects';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { Cell, Stack } from '@/lib/types';

vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: () => <div>keys body</div> }));
vi.mock('@/components/settings/EditingSection', () => ({ EditingSection: () => <div>editing body</div> }));
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: () => <div>theme body</div> }));
vi.mock('@/components/settings/ServersSection', () => ({ ServersSection: () => <div>servers body</div> }));
// The picker kinds, each its CellPanel with a search field, closing its
// session as the real ones do.
vi.mock('./runner/RunnerPicker', async () => {
    const { CellPanel } = await vi.importActual<typeof import('./cellPanel/CellPanel')>('./cellPanel/CellPanel');
    const { useCollimateStore: collimate } = await vi.importActual<typeof import('@/stores/useCollimateStore')>('@/stores/useCollimateStore');
    return {
        RunnerPicker: ({ cellId, host }: { cellId: string; host?: 'cell' | 'pane' }) => <CellPanel cellId={cellId} kind="runners" host={host} title="Runners"
            query="" onQuery={() => {}} placeholder="Search runners" rows={[]} onClose={() => collimate.getState().closePickSession(cellId, 'runners')} />,
    };
});
vi.mock('./cellPanel/FilesPanel', async () => {
    const { CellPanel } = await vi.importActual<typeof import('./cellPanel/CellPanel')>('./cellPanel/CellPanel');
    const { useCollimateStore: collimate } = await vi.importActual<typeof import('@/stores/useCollimateStore')>('@/stores/useCollimateStore');
    return {
        FilesPanel: ({ cell, host }: { cell: Cell; host?: 'cell' | 'pane' }) => <CellPanel cellId={String(cell.id)} kind="files" host={host} title="Files"
            query="" onQuery={() => {}} placeholder="Find file" rows={[]} onClose={() => collimate.getState().closePickSession(String(cell.id), 'files')} />,
    };
});
vi.mock('@/stores/useRunnerStore', () => ({ useRunnerCatalog: () => ({ data: [], error: null, busy: false, refresh: async () => {} }) }));
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
    fetchCellHistory: vi.fn().mockResolvedValue({ nodes: [], total_versions: 1, current: { node_id: null, version_cell_id: 'a' } }),
    fetchRenderersWithCell: vi.fn().mockResolvedValue([]),
    fetchRendererPreferences: vi.fn().mockResolvedValue({}),
    savePickSession: vi.fn().mockResolvedValue(undefined),
    deletePickSession: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('@/lib/api/client', async original => ({
    ...await original<Record<string, unknown>>(),
    requestJson: vi.fn(async () => ({ conflicts: [], verdicts: {}, pending: false })),
}));
import CellEditor from './CellEditor';

const cellA: Cell = { id: 'a', name: 'Offline editing', files: [fileFixture('a.txt', 'x', { language: 'plaintext' })] };
const frame: Stack = { id: 's1', name: 'Frame', cells: [cellA, { id: 'b', name: 'Second', files: [] }] };
const STACKS = [frame];

const makeScene = (over: Partial<CommandPaneScene> = {}): CommandPaneScene => ({
    displayStacks: STACKS, displaySelectedId: 's1', visibleTopIndices: {}, fanRange: null,
    selectDisplayId: vi.fn(), setFannedOutStackId: vi.fn(), actions: { winRotate: vi.fn() } as unknown as SceneActions,
    mainStacks: STACKS, selectedStackId: 's1', isZoomedOut: false, toggleZoom: vi.fn(),
    capabilities: { shown: false, toggle: vi.fn(), create: vi.fn() },
    ...over,
});

/** The selected cell `a` with the scene's run request, as StackItem mounts it. */
function SelectedCell() {
    useRunRequest({ displayStackId: 's1', cellId: 'a', isFront: true });
    return <div data-cell-id="a" style={{ width: 600, height: 400 }}>
        <CellEditor cell={cellA} onUpdate={vi.fn()} isActive />
    </div>;
}

const store = () => useCollimateStore.getState();
const pane = () => screen.getByRole('complementary', { name: 'Commands' });
const slot = () => pane().querySelector('[data-pane-panel-slot]') as HTMLElement;
const inCell = () => document.querySelector('[data-cell-panel-slot]');
const query = () => screen.getByRole('combobox', { name: 'Command' });

async function mount(scene = makeScene(), withCell = true) {
    await act(async () => {
        render(<FocusProvider>{withCell && <SelectedCell />}<CommandPane scene={scene} /></FocusProvider>);
    });
}

beforeEach(() => {
    const channel = { id: 'ch', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1', main_stacks: STACKS };
    useCollimateStore.setState({
        state: { active_channel_id: 'ch', channels: [channel] }, activeChannel: channel, cellCache: { a: cellA },
        commandPane: { ...defaultCommandPane(), open: true }, commandPanePanelSlot: null, selectedCellId: 'a',
        cellLayouts: {}, runsMenuOpen: false, tabDrag: null, dockDrag: null,
        runs: {}, stoppingRunIds: new Set(), runPhases: {}, lastFinishedByCell: {}, activeRunsEverywhere: [],
        pickSessions: {}, cellHistory: {}, cellDiffs: {}, pendingGeneratorRunnerStackId: null, maximizedOverlayCellId: null,
    } as never);
    (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
    (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
});
afterEach(() => {
    cleanup();
    useCollimateStore.setState({ selectedCellId: null, pickSessions: {}, commandPane: defaultCommandPane() } as never);
});

describe('CommandPane hosting the selected cell\'s picker', () => {
    it('shows the cell\'s Files panel in its slot as the picker view, the cell in its normal state (CP-4)', async () => {
        await mount();
        await act(async () => openFilesPanel('a'));
        expect(pane()).toHaveAttribute('data-pane-view', 'picker');
        const panel = within(slot()).getByRole('dialog', { name: 'Files' });
        expect(panel).toHaveAttribute('data-cell-panel-host', 'pane');
        expect(inCell()).toBeNull();
        expect(document.querySelector('[data-cell-panel-scrim]')).toBeNull();
        expect(screen.queryByRole('combobox', { name: 'Command' })).toBeNull();
        expect(screen.getByTitle('Commands act on this cell')).toHaveTextContent('Offline editing');
    });

    it('openFilesPanel puts the keyboard in the hosted panel\'s search, also when the panel is already open (CP-4)', async () => {
        await mount();
        await act(async () => openFilesPanel('a'));
        const search = within(slot()).getByRole('combobox', { name: 'Find file' });
        expect(document.activeElement).toBe(search);
        act(() => screen.getByRole('button', { name: 'Dock right' }).focus());
        expect(document.activeElement).not.toBe(search);
        await act(async () => openFilesPanel('a'));
        expect(document.activeElement).toBe(search);
    });

    it('Esc in the hosted panel closes its session and the pane\'s query takes the keyboard; the pane stays open (CP-4)', async () => {
        await mount();
        await act(async () => openFilesPanel('a'));
        await act(async () => { fireEvent.keyDown(within(slot()).getByRole('combobox', { name: 'Find file' }), { key: 'Escape' }); });
        expect(store().pickSessions.a).toBeUndefined();
        expect(store().commandPane.open).toBe(true);
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
        expect(inCell()).toBeNull();
        expect(document.activeElement).toBe(query());
    });

    it('Alt+R typed in the open pane hosts the runners panel there (CP-4)', async () => {
        const focusState = {
            focusArea: 'main', selectedWindowId: 's1', mainFocusMode: 'window',
            setFocusArea: vi.fn(), setSelectedWindowId: vi.fn(), setMainFocusMode: vi.fn(),
        } as unknown as FocusContextType;
        renderHook(() => useKeyboardHandler({
            focusState, mainStacks: STACKS, activeChannel: null, performAction: p => p,
            isZoomedOut: false, setIsZoomedOut: vi.fn(), toggleZoom: vi.fn(),
            setTransitionType: vi.fn(), setVisibleTopIndices: vi.fn(), setMergeNonces: vi.fn(),
            visibleTopIndicesRef: { current: {} }, handleRemoveStack: vi.fn(), actions: makeScene().actions,
            fannedOutStackId: null, setFannedOutStackId: vi.fn(), fan: null,
            displayStacks: STACKS, displaySelectedId: 's1', selectDisplayId: vi.fn(),
        }));
        await mount();
        await act(async () => { fireEvent.keyDown(query(), { key: 'r', code: 'KeyR', altKey: true }); });
        expect(store().pickSessions.a).toMatchObject({ kind: 'runners' });
        expect(pane()).toHaveAttribute('data-pane-view', 'picker');
        expect(within(slot()).getByRole('dialog', { name: 'Runners' })).toHaveAttribute('data-cell-panel-host', 'pane');
        expect(inCell()).toBeNull();
        expect(document.activeElement).toBe(within(slot()).getByRole('combobox', { name: 'Search runners' }));
    });

    it('closing the pane puts the panel back over the cell; reopening hosts it again (CP-4)', async () => {
        await mount();
        await act(async () => store().openPickSession('a', 'runners'));
        expect(within(slot()).getByRole('dialog', { name: 'Runners' })).toBeInTheDocument();
        await act(async () => store().closeCommandPane());
        expect(store().commandPanePanelSlot).toBeNull();
        expect(within(inCell() as HTMLElement).getByRole('dialog', { name: 'Runners' })).toHaveAttribute('data-cell-panel-host', 'cell');
        expect(document.querySelector('[data-cell-panel-scrim]')).not.toBeNull();
        await act(async () => store().openCommandPane());
        expect(within(slot()).getByRole('dialog', { name: 'Runners' })).toHaveAttribute('data-cell-panel-host', 'pane');
        expect(inCell()).toBeNull();
    });

    it('selecting another cell leaves the panel in its cell and the pane on its commands (CP-4)', async () => {
        await mount();
        await act(async () => openFilesPanel('a'));
        await act(async () => useCollimateStore.setState({ selectedCellId: 'b' }));
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
        expect(slot()).toBeEmptyDOMElement();
        expect(within(inCell() as HTMLElement).getByRole('dialog', { name: 'Files' })).toBeInTheDocument();
    });

    it('Settings opened over a hosted picker shows the settings view; the picker waits in its cell, its session untouched (CP-3)', async () => {
        await mount();
        await act(async () => openFilesPanel('a'));
        expect(pane()).toHaveAttribute('data-pane-view', 'picker');
        await act(async () => store().openCommandPaneSettings(null));
        expect(pane()).toHaveAttribute('data-pane-view', 'settings');
        expect(slot()).toBeEmptyDOMElement();
        expect(within(inCell() as HTMLElement).getByRole('dialog', { name: 'Files' })).toHaveAttribute('data-cell-panel-host', 'cell');
        expect(store().pickSessions.a).toMatchObject({ kind: 'files' });
        await act(async () => store().setCommandPaneView({ kind: 'commands' }));
        expect(pane()).toHaveAttribute('data-pane-view', 'picker');
        expect(within(slot()).getByRole('dialog', { name: 'Files' })).toHaveAttribute('data-cell-panel-host', 'pane');
    });

    it('in the overview no cell editor hosts, so the pane stays on its commands (CP-4)', async () => {
        useCollimateStore.setState({ pickSessions: { a: { kind: 'files', query: '', at: 1 } } });
        await mount(makeScene({ isZoomedOut: true }), false);
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
        expect(query()).toBeInTheDocument();
    });

    it('over version history the cell shows no panel, so the pane stays on its commands (CP-4)', async () => {
        useCollimateStore.setState({ pickSessions: { a: { kind: 'files', query: '', at: 1 } }, cellHistory: { a: { nodeId: 'n', versionCellId: 'v' } } } as never);
        await mount(makeScene(), false);
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
    });
});
