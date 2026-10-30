// A built-in card's cell editor.

import 'fake-indexeddb/auto';
import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import type { Cell, PickKind } from '@/lib/types';

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

// A built-in card (the head of the Capabilities stack) is not a read-only
// cell: its files are, yet Find file still opens. It never imports or
// exports, and never shows the runners panel (no run starts on Capabilities).

const card = (): Cell => ({ id: 'defs', name: 'Definitions', builtin: true, files: [fileFixture('renderer/renderer.handler', 'id: renderer', { language: 'yaml' })] });

async function mount(c: Cell, props: Record<string, unknown> = {}) {
    const channel = { id: 'ch-id', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: 's', main_stacks: [{ id: 's', name: 'Capabilities', stack_type: 'capabilities', cells: [c] }] };
    useCollimateStore.setState({ state: { active_channel_id: 'ch-id', channels: [channel] }, activeChannel: channel, cellCache: { [c.id]: c } } as never);
    await act(async () => {
        render(<FocusProvider><div style={{ width: 600, height: 400 }}><CellEditor cell={c} capabilitiesCell onUpdate={vi.fn()} {...props} /></div></FocusProvider>);
    });
}
const session = (kind: PickKind) => act(() => {
    useCollimateStore.setState({ pickSessions: { defs: { kind, query: '', at: 1 } } });
});

beforeEach(() => {
    useCollimateStore.setState({
        cellLayouts: {}, cellCache: {}, tabDrag: null, dockDrag: null,
        runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {}, cellDiffs: {},
        commandPane: defaultCommandPane(), commandPanePanelSlot: null, selectedCellId: 'defs',
    } as never);
    (URL as unknown as Record<string, unknown>).createObjectURL = () => 'blob:x';
    (URL as unknown as Record<string, unknown>).revokeObjectURL = () => {};
});
afterEach(() => { cleanup(); useCollimateStore.setState({ selectedCellId: null } as never); });

describe('CellEditor on a built-in card', () => {
    it('Find file opens; Import / export says the packages are the app\'s; there is no history', async () => {
        await mount(card());
        act(() => useCollimateStore.getState().updateCellLayout('defs', l => ({ ...l, filesPane: { ...l.filesPane, collapsed: false } })));
        expect(screen.getByRole('button', { name: 'Find file' })).toBeEnabled();
        const transfer = screen.getByRole('button', { name: 'Import / export' });
        expect(transfer).toBeDisabled();
        expect(transfer).toHaveAttribute('title', 'Built-in packages are part of the app');
        expect(screen.queryByRole('button', { name: 'History' })).toBeNull();
    });

    it('shows its Files panel, never the runners or transfer panel', async () => {
        await mount(card());
        await session('runners');
        expect(screen.queryByTestId('runners-panel')).toBeNull();
        await session('files');
        expect(screen.getByTestId('files-panel')).toBeInTheDocument();
        await session('transfer');
        expect(screen.queryByTestId('transfer-panel')).toBeNull();
    });

    it('a card in a read-only view (a cell diff) shows no panel at all', async () => {
        await mount(card(), { readOnly: true });
        await session('files');
        expect(screen.queryByTestId('files-panel')).toBeNull();
        act(() => useCollimateStore.getState().updateCellLayout('defs', l => ({ ...l, filesPane: { ...l.filesPane, collapsed: false } })));
        expect(screen.getByRole('button', { name: 'Find file' })).toBeDisabled();
    });
});
