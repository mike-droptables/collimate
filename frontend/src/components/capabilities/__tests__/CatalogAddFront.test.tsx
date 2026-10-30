// Add from catalog through the real StackGroup, manage card and catalog
// step, with the store's real applyOptimisticLayout: the added cell comes to
// the front whether the POST answer or a state push brings it first.
import { useEffect, useRef, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StackGroup, type StackCallbacks } from '@/components/StackGroup';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { slotOfCellIndex } from '@/lib/frontSlots';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { addFromCatalog, fetchCapabilityCatalog, fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchHandlers } from '@/lib/definitionApi';
import { fetchRunnerGenerators, type RunnerEntry } from '@/lib/runnerApi';
import type { Stack } from '@/lib/types';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})), putChannelSettings: vi.fn(() => Promise.resolve({})),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), addFromCatalog: vi.fn(), fetchCapabilityCatalog: vi.fn(),
}));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[] | null }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: async () => {} }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Stack width" /> }));
// A cell face: just its id, so the test reads which slot is in front.
vi.mock('@/components/StackItem', () => ({
    StackItem: (props: { cell: { id: string } }) => <div data-face-cell={props.cell.id} />,
}));

// The store's real action, kept before the fixture seeding mocks it.
const realApplyOptimisticLayout = useCollimateStore.getState().applyOptimisticLayout;

/** A minimal scene: the Capabilities stack from the store, its front slot
 *  in local state, onShowCell wired as winSetTopCell is. */
function Scene() {
    const stack = useCollimateStore(s => s.activeChannel?.main_stacks.find(x => x.id === 'caps')) as Stack;
    const [top, setTop] = useState(0);
    const stackRef = useRef(stack);
    useEffect(() => { stackRef.current = stack; }, [stack]);
    const callbacks: StackCallbacks = {
        onSelect() {}, onClose() {}, onRemoveCell() {}, onUpdate() {},
        onSetTopSlot: slot => setTop(slot),
        onShowCell: idx => setTop(slotOfCellIndex(stackRef.current, idx)),
        onShowManage: () => setTop(0),
    };
    return <FocusProvider>
        <StackGroup stack={stack} width={800} height={600} isSelected viewMode="stack" visibleTopIndex={top} callbacks={callbacks} />
    </FocusProvider>;
}

const frontFace = () => (document.querySelector('[data-manage-slot]') ? 'manage' : document.querySelector('[data-face-cell]')?.getAttribute('data-face-cell') ?? null);

/** The add's answer: the layout with 'charts-cell' appended to Capabilities. */
function layoutWithCharts() {
    const ch = useCollimateStore.getState().activeChannel!;
    return {
        ...ch,
        main_stacks: ch.main_stacks.map(st => (st.id === 'caps' ? { ...st, cells: [...st.cells, { id: 'charts-cell', name: 'Charts', files: [] }] } : st)),
        cell_id: 'charts-cell', package_skips: [],
    };
}

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel({ applyOptimisticLayout: realApplyOptimisticLayout });
    runnerMocks.rows = [runner()];
    vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([renderer(), builtinRenderer()]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([handler(), builtinHandler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
    vi.mocked(fetchCapabilityCatalog).mockResolvedValue([
        { id: 'starter', name: 'Starter', description: '', cells: [{ name: 'Charts', folder: 'views/charts', section: 'renderer', held: false }] },
    ]);
});
afterEach(cleanup);

async function clickAdd() {
    await screen.findByRole('article', { name: 'Memo handler' });
    expect(frontFace()).toBe('manage');
    fireEvent.click(screen.getByRole('button', { name: 'Add from catalog…' }));
    const list = await screen.findByRole('list', { name: 'Catalog cells' });
    fireEvent.click(within(list).getByRole('button', { name: 'Add' }));
}

describe('Add from catalog on the Capabilities stack', () => {
    it('fronts the added cell when the POST answers before any state push', async () => {
        vi.mocked(addFromCatalog).mockImplementation(async () => layoutWithCharts() as never);
        render(<Scene />);
        await clickAdd();
        await waitFor(() => expect(frontFace()).toBe('charts-cell'));
        expect(useCollimateStore.getState().activeChannel!.main_stacks[0].cells.map(c => c.id)).toContain('charts-cell');
    });

    it('fronts the added cell when a state push lands while the POST is in flight', async () => {
        let resolve!: (value: unknown) => void;
        vi.mocked(addFromCatalog).mockImplementation(() => new Promise(r => { resolve = r; }) as never);
        render(<Scene />);
        await clickAdd();
        const layout = layoutWithCharts();
        act(() => { realApplyOptimisticLayout(layout as never); });
        await waitFor(() => expect(Array.from(document.querySelectorAll<HTMLElement>('[data-scrub-slot]')).map(s => s.title)).toContain('Charts'));
        await act(async () => { resolve(layout); });
        await waitFor(() => expect(frontFace()).toBe('charts-cell'));
    });
});
