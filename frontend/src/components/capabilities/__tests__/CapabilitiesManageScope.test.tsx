import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CapabilitiesManageBody } from '../CapabilitiesManageBody';
import bodySource from '../CapabilitiesManageBody.tsx?raw';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell } from '@/lib/api';
import { fetchCapabilityLibraries, fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchChannelOutline } from '@/lib/api/cells';
import { fetchHandlers } from '@/lib/definitionApi';
import { BARE_KEYS_OWNER_ATTR, KEYBOARD_OWNER_ATTR, NAV_KEYS_OWNER_ATTR, sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { fetchRunnerGenerators, saveRunner, type RunnerDraft, type RunnerEntry } from '@/lib/runnerApi';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({
    ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn(), saveRunner: vi.fn(async () => ({})),
}));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilityCells', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityUpdates: vi.fn(async () => []), reviewReplace: vi.fn(), replaceFromUpstream: vi.fn(),
}));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), fetchCapabilityLibraries: vi.fn() }));
vi.mock('@/lib/api/cells', async original => ({ ...(await original<Record<string, unknown>>()), fetchChannelOutline: vi.fn() }));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[], refresh: vi.fn(async () => {}) }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: runnerMocks.refresh }),
}));
vi.mock('@/components/runner/RunnerEditor', () => ({
    RunnerEditor: ({ initial, onSave }: { initial?: RunnerDraft; onSave: (draft: RunnerDraft) => Promise<void> }) => <div data-testid="runner-editor">
        <span data-testid="opened-with">{initial ? initial.runner.generator : 'pick'}</span>
        <button type="button" onClick={() => void onSave({ runner: { ...(initial?.runner ?? { generator: 'synth-gen', description: '' }), id: 'nightly-1', name: 'Nightly' } as RunnerDraft['runner'], files: [] })}>Save draft</button>
    </div>,
}));

/** The scoped body as its Manage tab mounts it. */
function mount(cellId: string) {
    return render(<div data-testid="tab"><CapabilitiesManageBody scope={{ cellId }} /></div>);
}
const groupHeads = () => screen.queryAllByRole('heading', { level: 4 }).map(head => head.textContent);
const region = (name: RegExp) => screen.getByRole('region', { name });

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    runnerMocks.rows = [runner(), runner({ id: 'views-run', name: 'Views run', cell_id: 'views' })];
    vi.mocked(fetchGenerators).mockResolvedValue([generator(), generator({ name: 'view-gen', display_name: 'View generator', cell_id: 'views', group: 'Views' })]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([renderer(), builtinRenderer()]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([handler(), builtinHandler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([
        { cell_id: 'views', path: 'broken/broken.renderer', kind: 'renderer', error: 'No id' },
        { cell_id: 'tools', path: 'other/other.generator', kind: 'generator', error: 'No entrypoint' },
    ]);
    vi.mocked(fetchCapabilityLibraries).mockResolvedValue([]);
    vi.mocked(fetchChannelOutline).mockResolvedValue({ capabilities_cells: [{ id: 'tools', name: 'Tools' }, { id: 'views', name: 'Views' }], main_stacks: [] } as never);
});
afterEach(cleanup);

describe('a Capabilities cell\'s Manage tab', () => {
    it('lists only what its cell holds, problems included', async () => {
        mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        expect(groupHeads()).toEqual(['Generators · 1', 'Runners · 1', 'Renderers · 1', 'Handlers · 1', 'Libraries · 0']);
        expect(screen.getByText('View generator')).toBeInTheDocument();
        expect(screen.queryByText('Synth generator')).toBeNull();
        expect(screen.getByRole('article', { name: 'Views run runner' })).toBeInTheDocument();
        expect(screen.queryByRole('article', { name: 'Synth run runner' })).toBeNull();
        expect(within(region(/^Renderers/)).getByRole('button', { name: '.memo Memo view' })).toBeInTheDocument();
        // The other cell's built-ins are not here, folded or not.
        expect(screen.queryByRole('button', { name: /^Built-in/ })).toBeNull();
        const problems = await screen.findByRole('region', { name: 'Needs attention' });
        expect(problems).toHaveTextContent('1 file here isn’t a working capability');
        fireEvent.click(within(problems).getByRole('button', { name: /working capability/ }));
        expect(within(problems).getByText('broken/broken.renderer')).toBeInTheDocument();
        expect(within(problems).queryByText('other/other.generator')).toBeNull();
    });

    it('says a group is empty in this cell', async () => {
        vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
        runnerMocks.rows = [runner()];
        mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        expect(region(/^Generators/)).toHaveTextContent('No generators in this cell.');
        expect(region(/^Runners/)).toHaveTextContent('No runners yet.');
    });

    it('has no Add from catalog… and no Open cell; Create… and Open file stay', async () => {
        mount('views');
        const card = await screen.findByRole('article', { name: 'Memo handler' });
        expect(screen.queryByRole('button', { name: 'Add from catalog…' })).toBeNull();
        expect(screen.getByRole('button', { name: 'Create…' })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Open cell' })).toBeNull();
        expect(card.querySelector('[data-action="openCell"]')).toBeNull();
        expect(card.querySelector('[data-action="openFile"]')).not.toBeNull();
        // A generator row is no door to its cell: no button, no cell chip.
        expect(screen.queryByRole('button', { name: /open its cell/ })).toBeNull();
        expect(screen.queryByTitle('Bring its cell to the front')).toBeNull();
    });

    it('adds a runner into its cell with no Keep it in step, from the group and from a generator', async () => {
        mount('tools');
        await screen.findByRole('article', { name: 'Synth run runner' });
        fireEvent.click(within(region(/^Runners/)).getByRole('button', { name: 'Add runner' }));
        expect(screen.queryByRole('group', { name: 'Keep it in' })).toBeNull();
        fireEvent.click(await screen.findByRole('button', { name: 'Save draft' }));
        await waitFor(() => expect(saveRunner).toHaveBeenCalledWith(expect.anything(), false, { cellId: 'tools' }));
        expect(fetchChannelOutline).not.toHaveBeenCalled();

        await screen.findByRole('article', { name: 'Synth run runner' });
        fireEvent.click(within(screen.getByText('Synth generator').closest('[data-generator-row]') as HTMLElement).getByRole('button', { name: 'Add runner' }));
        expect(screen.queryByRole('group', { name: 'Keep it in' })).toBeNull();
        expect(await screen.findByTestId('opened-with')).toHaveTextContent('synth-gen');
        fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
        await waitFor(() => expect(saveRunner).toHaveBeenCalledTimes(2));
        expect(vi.mocked(saveRunner).mock.calls[1][2]).toEqual({ cellId: 'tools' });
    });

    it('on a built-in card opens the Built-in disclosures and offers no Add runner', async () => {
        mount('defs');
        await screen.findByRole('article', { name: 'Sheet handler' });
        expect(within(region(/^Renderers/)).getByRole('button', { name: '.txt Plain' })).toBeInTheDocument();
        for (const disclosure of screen.getAllByRole('button', { name: 'Built-in (1)' })) {
            expect(disclosure).toHaveAttribute('aria-expanded', 'true');
        }
        expect(screen.queryByRole('article', { name: 'Memo handler' })).toBeNull();
        expect(within(region(/^Runners/)).queryByRole('button', { name: 'Add runner' })).toBeNull();
        expect(screen.getByRole('article', { name: 'Sheet handler' }).querySelector('[data-action="openCard"]')).toBeNull();
    });

    it('Create… reveals a fit another cell holds on the master face, and opens its own here', async () => {
        const createFor = async (ext: string) => {
            fireEvent.click(screen.getByRole('button', { name: 'Create…' }));
            fireEvent.click(await screen.findByRole('radio', { name: /^Understand a new kind of file/ }));
            fireEvent.click(screen.getByRole('button', { name: 'Next' }));
            fireEvent.change(screen.getByRole('textbox', { name: 'File extension' }), { target: { value: ext } });
        };
        const view = mount('tools');
        await screen.findByRole('article', { name: 'Synth run runner' });
        await createFor('memo');
        fireEvent.click(within(screen.getByRole('status')).getByRole('button', { name: 'Add an example' }));
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({
            kind: 'handler', handler: { cellId: 'views', folder: 'memo', step: 'addExample' },
        });
        expect(screen.queryByRole('group', { name: 'What should this channel learn to do?' })).toBeNull();
        expect(groupHeads()).toHaveLength(5);
        view.unmount();

        useCollimateStore.setState({ capabilitiesManageRequest: null });
        mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        await createFor('memo');
        fireEvent.click(within(screen.getByRole('status')).getByRole('button', { name: 'Add an example' }));
        expect(await screen.findByRole('region', { name: 'Add an example to ‘Memo’' })).toBeInTheDocument();
        expect(groupHeads()).toEqual(['Handlers · 1']);
        expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull();
    });

    it('never reads or clears the store\'s request: that is the master face\'s', async () => {
        useCollimateStore.setState({ capabilitiesManageRequest: { seq: 7, kind: 'handler', query: 'memo' } });
        mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ seq: 7 });
        expect(screen.getByRole('searchbox', { name: 'Search capabilities' })).toHaveValue('');
        expect(groupHeads()).toHaveLength(5);
    });

    it('its search owns the Esc that clears a query; the next Esc is the scene\'s', async () => {
        mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        const search = screen.getByRole('searchbox', { name: 'Search capabilities' });
        const esc = () => {
            let seen: boolean | null = null;
            const listen = (e: KeyboardEvent) => { seen = sceneKeyboardOwned(e); };
            window.addEventListener('keydown', listen, true);
            try { fireEvent.keyDown(search, { key: 'Escape', code: 'Escape' }); } finally { window.removeEventListener('keydown', listen, true); }
            return seen;
        };
        act(() => { search.focus(); });
        fireEvent.change(search, { target: { value: 'yaml' } });
        expect(esc()).toBe(true);
        expect(search).toHaveValue('');
        expect(search).toHaveFocus();
        expect(esc()).toBe(false);
    });

    it('owns no keys and never takes the keyboard by itself', async () => {
        const { getByTestId } = mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        await new Promise(r => requestAnimationFrame(() => r(null)));
        const search = screen.getByRole('searchbox', { name: 'Search capabilities' });
        expect(document.activeElement).not.toBe(search);
        const tab = getByTestId('tab');
        for (const attr of [BARE_KEYS_OWNER_ATTR, KEYBOARD_OWNER_ATTR, NAV_KEYS_OWNER_ATTR]) {
            expect(tab.querySelector(`[${attr}]`)).toBeNull();
        }
    });

    it('narrows the search inside the cell', async () => {
        mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        fireEvent.change(screen.getByRole('searchbox', { name: 'Search capabilities' }), { target: { value: 'tools' } });
        // Every match of 'tools' is held by the Tools cell: nothing here.
        expect(screen.getByText('Nothing matches ‘tools’.')).toBeInTheDocument();
    });

    it('paints only from the skin tokens', async () => {
        const { container } = mount('views');
        await screen.findByRole('article', { name: 'Memo handler' });
        expect(container.innerHTML).not.toMatch(RETIRED_TOKENS);
        expect(bodySource).not.toMatch(/#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?)\(/);
    });
});
