import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CapabilitiesManageCard } from '../CapabilitiesManageCard';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchChannelOutline } from '@/lib/api/cells';
import { fetchHandlers } from '@/lib/definitionApi';
import {
    deleteRunner, duplicateRunner, fetchRunner, fetchRunnerGenerators, saveRunner, type RunnerDraft, type RunnerEntry,
} from '@/lib/runnerApi';
import { generator, handler, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn(), saveRunner: vi.fn(async () => ({})), duplicateRunner: vi.fn(async () => ({})), deleteRunner: vi.fn(async () => ({})),
}));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn() }));
vi.mock('@/lib/api/cells', async original => ({ ...(await original<Record<string, unknown>>()), fetchChannelOutline: vi.fn() }));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[], refresh: vi.fn(async () => {}) }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: runnerMocks.refresh }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => null }));
// The editor itself is RunnerEditor's own test's; here it shows what it was
// opened with and saves a named draft from it.
vi.mock('@/components/runner/RunnerEditor', () => ({
    RunnerEditor: ({ initial, onSave, onCancel }: { initial?: RunnerDraft; onSave: (draft: RunnerDraft) => Promise<void>; onCancel: () => void }) => <div data-testid="runner-editor">
        <span data-testid="opened-with">{initial ? `${initial.runner.generator}|${initial.runner.id}|${initial.runner.description}` : 'pick'}</span>
        <button type="button" onClick={() => void onSave({ runner: { ...(initial?.runner ?? { generator: 'synth-gen', description: '' }), id: initial?.runner.id || 'nightly-1', name: 'Nightly' } as RunnerDraft['runner'], files: [] })}>Save draft</button>
        <button type="button" onClick={onCancel}>Cancel draft</button>
    </div>,
}));

const showCell = vi.fn();
async function mount() {
    render(<CapabilitiesManageCard stackId="caps" width={480} height={640} isSelected isFront onSelect={vi.fn()} onShowCell={showCell} onHide={vi.fn()} />);
    await screen.findByRole('article', { name: 'Synth run runner' });
}
const runners = () => screen.getByRole('region', { name: /^Runners · / });

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    runnerMocks.rows = [runner(), runner({ id: 'broken', name: 'Broken run', description: '', tags: [], invalid: { reason: 'Target generator is unavailable' } }),
        runner({ id: 'unset', name: 'Unset run', description: '', tags: [], missing_required: [{ key: 'key', label: 'API key' }] })];
    vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([handler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
    vi.mocked(fetchChannelOutline).mockResolvedValue({
        capabilities_cells: [{ id: 'tools', name: 'Tools' }, { id: 'views', name: 'Views' }],
        main_stacks: [],
    });
});
afterEach(cleanup);

describe('The Runners group', () => {
    it('shows each runner’s status: Ready, Needs setup or Broken', async () => {
        await mount();
        expect(within(screen.getByRole('article', { name: 'Synth run runner' })).getByText('Ready')).toHaveAttribute('data-runner-status', 'ready');
        expect(within(screen.getByRole('article', { name: 'Broken run runner' })).getByText('Broken: Target generator is unavailable')).toBeInTheDocument();
        expect(within(screen.getByRole('article', { name: 'Unset run runner' })).getByText('Needs setup: API key')).toBeInTheDocument();
        expect(screen.getByRole('article', { name: 'Synth run runner' })).toHaveTextContent('nightly');
    });

    it('Add runner asks where to keep it, its generator’s cell by default, then saves there', async () => {
        await mount();
        fireEvent.click(within(runners()).getByRole('button', { name: 'Add runner' }));
        const keep = await screen.findByRole('group', { name: 'Keep it in' });
        await waitFor(() => expect(within(keep).getAllByRole('radio')).toHaveLength(4));
        expect(within(keep).getAllByRole('radio').map(radio => radio.closest('label')!.textContent)).toEqual(['Its generator’s cell', 'Tools', 'Views', 'New cell']);
        expect(within(keep).getByRole('radio', { name: 'Its generator’s cell' })).toBeChecked();
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByTestId('opened-with')).toHaveTextContent('pick');
        fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
        await waitFor(() => expect(saveRunner).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ name: 'Nightly' }) }), false, undefined));
        expect(runnerMocks.refresh).toHaveBeenCalled();
        expect(await screen.findByRole('status')).toHaveTextContent('Runner ‘Nightly’ added.');
        expect(screen.queryByTestId('runner-editor')).toBeNull();
    });

    it('a runner kept in another Capabilities cell is saved into it', async () => {
        await mount();
        fireEvent.click(within(runners()).getByRole('button', { name: 'Add runner' }));
        fireEvent.click(await screen.findByRole('radio', { name: 'Views' }));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
        await waitFor(() => expect(saveRunner).toHaveBeenCalledWith(expect.anything(), false, { cellId: 'views' }));
    });

    it('a runner kept in a New cell asks the server for one', async () => {
        await mount();
        fireEvent.click(within(runners()).getByRole('button', { name: 'Add runner' }));
        const fresh = await screen.findByRole('radio', { name: 'New cell' });
        fireEvent.click(fresh);
        expect(screen.getByText('A new cell at the end of Capabilities, named after the runner.')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
        await waitFor(() => expect(saveRunner).toHaveBeenCalledWith(expect.anything(), false, { newCell: true }));
    });

    it('a generator’s Add runner preselects it in the editor', async () => {
        await mount();
        const row = document.querySelector('[data-generator-row="synth-gen"]') as HTMLElement;
        fireEvent.click(within(row).getByRole('button', { name: 'Add runner' }));
        fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
        expect(screen.getByTestId('opened-with')).toHaveTextContent('synth-gen||The default description');
        fireEvent.click(screen.getByRole('button', { name: 'Cancel draft' }));
        expect(screen.getByRole('searchbox', { name: 'Search capabilities' })).toBeVisible();
    });

    it('Edit opens the stored runner and saves it where it lives', async () => {
        vi.mocked(fetchRunner).mockResolvedValue({ runner: { ...runner(), id: 'synth-run' }, files: [], revision: 'r1', cell_id: 'tools', generator: runnerGenerator() });
        await mount();
        fireEvent.click(within(screen.getByRole('article', { name: 'Synth run runner' })).getByRole('button', { name: 'Edit' }));
        expect(await screen.findByTestId('opened-with')).toHaveTextContent('synth-gen|synth-run|');
        expect(screen.getByText('Edit ‘Synth run’')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
        await waitFor(() => expect(saveRunner).toHaveBeenCalledWith(expect.objectContaining({ runner: expect.objectContaining({ id: 'synth-run' }) }), true));
        expect(await screen.findByRole('status')).toHaveTextContent('Runner ‘Nightly’ saved.');
    });

    it('Duplicate copies it under a fresh name beside the original', async () => {
        await mount();
        fireEvent.click(within(screen.getByRole('article', { name: 'Synth run runner' })).getByRole('button', { name: 'Duplicate' }));
        await waitFor(() => expect(duplicateRunner).toHaveBeenCalledWith('synth-run', ['Synth run', 'Broken run', 'Unset run']));
        expect(runnerMocks.refresh).toHaveBeenCalled();
    });

    it('Delete confirms in a strip naming the cell its files leave', async () => {
        await mount();
        const card = screen.getByRole('article', { name: 'Synth run runner' });
        fireEvent.click(within(card).getByRole('button', { name: 'Delete' }));
        expect(within(card).getByText('Delete ‘Synth run’? Its files leave Tools.').parentElement!.className).toContain('bg-red-soft');
        fireEvent.click(within(card).getByRole('button', { name: 'Keep' }));
        expect(deleteRunner).not.toHaveBeenCalled();
        fireEvent.click(within(card).getByRole('button', { name: 'Delete' }));
        fireEvent.click(within(card).getAllByRole('button', { name: 'Delete' })[1]);
        await waitFor(() => expect(deleteRunner).toHaveBeenCalledWith('synth-run'));
        expect(runnerMocks.refresh).toHaveBeenCalled();
    });

    it('Open cell brings the runner’s cell to the front', async () => {
        await mount();
        fireEvent.click(within(screen.getByRole('article', { name: 'Synth run runner' })).getByRole('button', { name: 'Open cell' }));
        expect(showCell).toHaveBeenCalledWith('tools');
    });

    it('with no runners it says why: no generator yet, or none made', async () => {
        runnerMocks.rows = [];
        vi.mocked(fetchGenerators).mockResolvedValue([]);
        render(<CapabilitiesManageCard stackId="caps" width={480} height={640} isSelected isFront onSelect={vi.fn()} onShowCell={showCell} onHide={vi.fn()} />);
        expect(await screen.findByText('Runners start a generator. Add a generator first.')).toBeInTheDocument();
        expect(screen.getByText('No generators yet. Create one, or add a .generator from any cell’s Files panel.')).toBeInTheDocument();
        cleanup();
        vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
        render(<CapabilitiesManageCard stackId="caps" width={480} height={640} isSelected isFront onSelect={vi.fn()} onShowCell={showCell} onHide={vi.fn()} />);
        expect(await screen.findByText('No runners yet.')).toBeInTheDocument();
    });

    it('with no generator, Add runner is off and the group offers Create… instead', async () => {
        runnerMocks.rows = [];
        vi.mocked(fetchGenerators).mockResolvedValue([]);
        render(<CapabilitiesManageCard stackId="caps" width={480} height={640} isSelected isFront onSelect={vi.fn()} onShowCell={showCell} onHide={vi.fn()} />);
        await screen.findByText('Runners start a generator. Add a generator first.');
        const add = within(runners()).getByRole('button', { name: 'Add runner' });
        expect(add).toBeDisabled();
        expect(add).toHaveAttribute('title', 'Runners start a generator: add a generator first');
        fireEvent.click(within(runners()).getByRole('button', { name: 'Create…' }));
        expect(await screen.findByRole('group', { name: 'What should this channel learn to do?' })).toBeInTheDocument();
    });

    it('a runner-add request with no generator shows the Runners group, never the dead-end Add flow', async () => {
        runnerMocks.rows = [];
        vi.mocked(fetchGenerators).mockResolvedValue([]);
        render(<CapabilitiesManageCard stackId="caps" width={480} height={640} isSelected isFront onSelect={vi.fn()} onShowCell={showCell} onHide={vi.fn()} />);
        await screen.findByText('Runners start a generator. Add a generator first.');
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ kind: 'runner', runner: { action: 'add' } }); });
        await waitFor(() => expect(screen.queryByRole('group', { name: 'Keep it in' })).toBeNull());
        expect(screen.getByText('Runners start a generator. Add a generator first.')).toBeVisible();
        expect(within(runners()).getByRole('button', { name: 'Create…' })).toBeInTheDocument();
        expect(screen.queryByRole('region', { name: /^Generators · / })).toBeNull();
    });
});
