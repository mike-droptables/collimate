// The cell editor hands the selected cell's picker (Files, runners) to the
// Commands pane while the pane is open: the panel renders in the pane's
// slot (paneHostsPanel) and the cell shows its normal state. Everything
// else stays in the cell.

import 'fake-indexeddb/auto';
import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, within } from '@testing-library/react';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import type { Cell, PickKind } from '@/lib/types';

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
// Each kind's panel, reduced to a probe of where it renders and as what,
// holding a draft in its own state (as a runner editor holds its draft).
const probe = vi.hoisted(() => (kind: string) => function Probe({ host = 'cell' }: { host?: string }) {
    const [draft, setDraft] = useState('');
    return (
        <div data-testid={`${kind}-panel`} data-host={host}>
            <input aria-label={`${kind} draft`} value={draft} onChange={e => setDraft(e.target.value)} />
        </div>
    );
});
vi.mock('./runner/RunnerPicker', () => ({ RunnerPicker: probe('runners') }));
vi.mock('./cellPanel/FilesPanel', () => ({ FilesPanel: probe('files') }));
vi.mock('@/components/cellPanel/TransferPanel', () => ({ TransferPanel: probe('transfer') }));
vi.mock('@/stores/useRunnerStore', () => ({ useRunnerCatalog: () => ({ data: [], error: null, busy: false, refresh: async () => {} }) }));
vi.mock('@/hooks/useRendererId', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    useSupportedExtensions: () => [{ ext: '.txt', renderers: ['Text'] }],
}));
vi.mock('@/hooks/useZoomTransition', () => ({ useIsZoomTransitioning: () => false }));
vi.mock('@/hooks/useIdleCheckpoint', () => ({
    useIdleCheckpoint: () => ({ notifyEdit: vi.fn(), flushCheckpoint: vi.fn() }),
}));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    listCellVersions: vi.fn().mockResolvedValue({ versions: [] }),
    fetchCellHistory: vi.fn().mockResolvedValue({ nodes: [], total_versions: 1, current: { node_id: null, version_cell_id: 'cell-1' } }),
    savePickSession: vi.fn().mockResolvedValue(undefined),
    deletePickSession: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('@/lib/api/client', async original => ({
    ...await original<Record<string, unknown>>(),
    requestJson: vi.fn(async () => ({ conflicts: [], verdicts: {}, pending: false })),
}));
import CellEditor from './CellEditor';

const cell = (): Cell => ({ id: 'cell-1', name: 'Test cell', files: [fileFixture('a.txt', 'x', { language: 'plaintext' })] });
const store = () => useCollimateStore.getState();

/** The pane's slot, as the open pane registers it. */
let slot: HTMLElement;

function seed(c: Cell) {
    const channel = { id: 'ch-id', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: 's', main_stacks: [{ id: 's', name: 'S', cells: [c] }] };
    useCollimateStore.setState({ state: { active_channel_id: 'ch-id', channels: [channel] }, activeChannel: channel, cellCache: { [c.id]: c } } as never);
}
const mount = async (props: Record<string, unknown> = {}) => {
    const c = cell();
    seed(c);
    let view!: ReturnType<typeof render>;
    await act(async () => {
        view = render(<FocusProvider><div style={{ width: 600, height: 400 }}><CellEditor cell={c} onUpdate={vi.fn()} {...props} /></div></FocusProvider>);
    });
    return view;
};
const session = (kind: PickKind) => act(() => {
    useCollimateStore.setState({ pickSessions: { 'cell-1': { kind, query: '', at: 1 } } });
});
const inCell = () => document.querySelector('[data-cell-panel-slot]');
const openPane = () => act(() => {
    store().openCommandPane();
    store().setCommandPanePanelSlot(slot);
});

beforeEach(() => {
    slot = document.createElement('div');
    slot.setAttribute('data-pane-panel-slot', '');
    document.body.append(slot);
    conceal.open.length = 0;
    useCollimateStore.setState({
        cellLayouts: {}, cellCache: {}, tabDrag: null, dockDrag: null,
        runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {}, cellDiffs: {},
        commandPane: defaultCommandPane(), commandPanePanelSlot: null, selectedCellId: 'cell-1',
    } as never);
    (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
    (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
});
afterEach(() => {
    cleanup();
    slot.remove();
    useCollimateStore.setState({ selectedCellId: null, commandPanePanelSlot: null, commandPane: defaultCommandPane() } as never);
});

describe('CellEditor with the Commands pane open', () => {
    it('renders a files or runners session of the selected cell in the pane\'s slot as a pane panel, absent from the cell (CP-4)', async () => {
        await openPane();
        await mount();
        for (const kind of ['files', 'runners'] as const) {
            await session(kind);
            const panel = within(slot).getByTestId(`${kind}-panel`);
            expect(panel).toHaveAttribute('data-host', 'pane');
            expect(inCell()).toBeNull();
        }
    });

    it('leaves the cell in its normal state: nothing concealed (CP-4)', async () => {
        await openPane();
        await mount();
        await session('runners');
        expect(within(slot).getByTestId('runners-panel')).toBeInTheDocument();
        expect(conceal.open.at(-1)).toBe(false);
    });

    it('puts the panel back in the cell when the pane closes (its slot cleared), concealing as there, the session untouched (CP-4)', async () => {
        await openPane();
        await mount();
        await session('files');
        expect(within(slot).getByTestId('files-panel')).toBeInTheDocument();
        await act(() => {
            store().closeCommandPane();
            store().setCommandPanePanelSlot(null);
        });
        expect(slot).toBeEmptyDOMElement();
        expect(within(inCell() as HTMLElement).getByTestId('files-panel')).toHaveAttribute('data-host', 'cell');
        expect(conceal.open.at(-1)).toBe(true);
        expect(store().pickSessions['cell-1']).toMatchObject({ kind: 'files' });
    });

    it('puts the panel back in the cell when another cell is selected, and hosts it again when this one is (CP-4)', async () => {
        await openPane();
        await mount();
        await session('runners');
        await act(() => useCollimateStore.setState({ selectedCellId: 'cell-2' }));
        expect(slot).toBeEmptyDOMElement();
        expect(within(inCell() as HTMLElement).getByTestId('runners-panel')).toHaveAttribute('data-host', 'cell');
        await act(() => useCollimateStore.setState({ selectedCellId: 'cell-1' }));
        expect(within(slot).getByTestId('runners-panel')).toHaveAttribute('data-host', 'pane');
        expect(inCell()).toBeNull();
    });

    it('keeps the panel mounted as it moves between the pane and the cell: its unsaved state survives (CP-4)', async () => {
        await openPane();
        await mount();
        await session('runners');
        fireEvent.change(within(slot).getByLabelText('runners draft'), { target: { value: 'half-typed' } });
        await act(() => {
            store().closeCommandPane();
            store().setCommandPanePanelSlot(null);
        });
        expect(within(inCell() as HTMLElement).getByLabelText('runners draft')).toHaveValue('half-typed');
        await openPane();
        expect(within(slot).getByLabelText('runners draft')).toHaveValue('half-typed');
        await act(() => useCollimateStore.setState({ selectedCellId: 'cell-2' }));
        await act(() => useCollimateStore.setState({ selectedCellId: 'cell-1' }));
        expect(within(slot).getByLabelText('runners draft')).toHaveValue('half-typed');
    });

    it('never hosts the transfer panel: it stays over the cell (CP-4)', async () => {
        await openPane();
        await mount();
        await session('transfer');
        expect(within(inCell() as HTMLElement).getByTestId('transfer-panel')).toHaveAttribute('data-host', 'cell');
        expect(slot).toBeEmptyDOMElement();
        expect(conceal.open.at(-1)).toBe(true);
    });

    it('hosts nothing over a cell diff, where the cell shows no panel (CP-4)', async () => {
        await openPane();
        useCollimateStore.setState({ cellDiffs: { 'cell-1': 'cell-0' } } as never);
        await mount({ readOnly: true });
        await session('files');
        expect(slot).toBeEmptyDOMElement();
        expect(inCell()).toBeNull();
    });

    it('with the pane closed, the picker is the cell\'s (CP-4)', async () => {
        await mount();
        await session('runners');
        expect(within(inCell() as HTMLElement).getByTestId('runners-panel')).toHaveAttribute('data-host', 'cell');
        expect(conceal.open.at(-1)).toBe(true);
    });
});
