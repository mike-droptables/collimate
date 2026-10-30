import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CapabilitiesManageCard, type CapabilitiesManageCardProps } from '../CapabilitiesManageCard';
import cardSource from '../CapabilitiesManageCard.tsx?raw';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell, RENDERER_PREFS_CHANGED_EVENT } from '@/lib/api';
import { addFromCatalog, fetchCapabilityCatalog, fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchHandlers } from '@/lib/definitionApi';
import { BARE_KEYS_OWNER_ATTR, KEYBOARD_OWNER_ATTR, sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { fetchRunner, fetchRunnerGenerators, type RunnerEntry } from '@/lib/runnerApi';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { builtinHandler, builtinRenderer, generator, handler, renderer, runner, runnerGenerator, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), addFromCatalog: vi.fn(), fetchCapabilityCatalog: vi.fn(),
}));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[] | null, error: null as string | null, refresh: vi.fn(async () => {}) }));
vi.mock('@/stores/useRunnerStore', () => ({
    useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: runnerMocks.error, refresh: runnerMocks.refresh }),
}));
vi.mock('@/components/StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Stack width" /> }));
vi.mock('@/components/runner/RunnerEditor', () => ({
    RunnerEditor: ({ initial }: { initial?: { runner: { id: string; generator: string } } }) =>
        <div data-testid="runner-editor">{initial ? `${initial.runner.generator}:${initial.runner.id}` : 'pick'}</div>,
}));

const props = (over: Partial<CapabilitiesManageCardProps> = {}): CapabilitiesManageCardProps => ({
    stackId: 'caps', width: 480, height: 640, isSelected: true, isFront: true,
    onSelect: vi.fn(), onShowCell: vi.fn(), onHide: vi.fn(), ...over,
});

async function mount(over: Partial<CapabilitiesManageCardProps> = {}) {
    const p = props(over);
    const view = render(<CapabilitiesManageCard {...p} />);
    await screen.findByRole('article', { name: 'Memo handler' });
    return { ...view, props: p, rerenderWith: (next: Partial<CapabilitiesManageCardProps>) => view.rerender(<CapabilitiesManageCard {...p} {...next} />) };
}
const search = () => screen.getByRole('searchbox', { name: 'Search capabilities' });
const groupHeads = () => screen.queryAllByRole('heading', { level: 4 }).map(head => head.textContent);
const chip = (name: string) => within(screen.getByRole('group', { name: 'Show kinds' })).getByRole('button', { name: new RegExp(`^${name}`) });

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    runnerMocks.rows = [runner()];
    runnerMocks.error = null;
    vi.mocked(fetchGenerators).mockResolvedValue([generator()]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([runnerGenerator()]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([renderer(), builtinRenderer()]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([handler(), builtinHandler()]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
});
afterEach(cleanup);

describe('The Capabilities manage card', () => {
    it('is the stack’s card: an owner of its bare keys with a Manage head, Width and Hide', async () => {
        const { props: p, container } = await mount();
        const root = container.querySelector('[data-capabilities-manage]') as HTMLElement;
        expect(root).toHaveAttribute('data-stack-card');
        expect(root).toHaveAttribute(BARE_KEYS_OWNER_ATTR);
        expect(root).not.toHaveAttribute(KEYBOARD_OWNER_ATTR);
        expect(root).toHaveAttribute('tabindex', '-1');
        expect(within(root).getByText('Manage')).toBeInTheDocument();
        expect(within(root).getByRole('button', { name: 'Stack width' })).toBeInTheDocument();
        const hide = within(root).getByRole('button', { name: 'Hide Capabilities' });
        expect(hide).toHaveAttribute('title', 'Hide Capabilities to the top bar');
        fireEvent.click(hide);
        expect(p.onHide).toHaveBeenCalledTimes(1);
        // A press anywhere selects the stack.
        fireEvent.pointerDown(root, { button: 0 });
        expect(p.onSelect).toHaveBeenCalled();
    });

    it('says what the stack is, open until folded', async () => {
        await mount();
        const about = screen.getByRole('region', { name: 'About Capabilities' });
        expect(about).toHaveAttribute('data-capabilities-concepts');
        expect(about).toHaveTextContent('What this channel can make, show and check. Everything on this stack is in use, whichever cell holds it — the cell only keeps it organised. Nothing runs here.');
        expect(about).toHaveTextContent('Generators make files: a program, a terminal or an agent that works in a cell.');
        expect(about).toHaveTextContent('Runners are saved ways to start a generator: its settings, accounts and prompt. Use Run on any other cell to pick one.');
        expect(about).toHaveTextContent('Renderers show files: a view for one or more file types.');
        expect(about).toHaveTextContent('Handlers teach agents a file type and check its files, with checks and skills you review before they are used.');
        expect(about).toHaveTextContent('Build and Create open a work stack beside this one. Add what they make from that stack’s Files panel. Move a cell off this stack to set its capabilities aside.');
        fireEvent.click(within(about).getByRole('button', { name: 'About Capabilities' }));
        expect(about).not.toHaveTextContent('Nothing runs here.');
    });

    it('lists the groups in order with their subtitles; built-ins sit in a folded disclosure', async () => {
        await mount();
        expect(groupHeads()).toEqual(['Generators · 1', 'Runners · 1', 'Renderers · 2', 'Handlers · 2']);
        for (const subtitle of ['Make files.', 'Start a generator with saved settings.', 'Show files.', 'Check files and teach agents.']) {
            expect(screen.getByText(subtitle)).toBeInTheDocument();
        }
        const renderers = screen.getByRole('region', { name: 'Renderers · 2' });
        expect(within(renderers).getByRole('button', { name: '.memo Memo view' })).toBeInTheDocument();
        expect(within(renderers).queryByRole('button', { name: '.txt Plain' })).toBeNull();
        fireEvent.click(within(renderers).getByRole('button', { name: 'Built-in (1)' }));
        expect(within(renderers).getByRole('button', { name: '.txt Plain' })).toBeInTheDocument();
        expect(screen.queryByRole('article', { name: 'Sheet handler' })).toBeNull();
        // The runner card says what it runs and where it lives.
        const run = screen.getByRole('article', { name: 'Synth run runner' });
        expect(run).toHaveTextContent('runs Synth generator');
        expect(within(run).getByText('Ready')).toBeInTheDocument();
    });

    it('searches every term, casefolded, and says how many of each group match', async () => {
        await mount();
        fireEvent.change(search(), { target: { value: 'MEMO shows' } });
        expect(groupHeads()).toEqual(['Renderers · 1 of 2']);
        fireEvent.change(search(), { target: { value: 'sheet' } });
        // A matching built-in shows unfolded while searching.
        expect(screen.getByRole('article', { name: 'Sheet handler' })).toBeInTheDocument();
        expect(groupHeads()).toEqual(['Handlers · 1 of 2']);
        // The holding cell's name matches too.
        fireEvent.change(search(), { target: { value: 'tools' } });
        expect(groupHeads()).toEqual(['Generators · 1 of 1', 'Runners · 1 of 1']);
    });

    it('Esc in the search clears it, then blurs it', async () => {
        await mount();
        const box = search();
        box.focus();
        fireEvent.change(box, { target: { value: 'memo' } });
        fireEvent.keyDown(box, { key: 'Escape' });
        expect(box).toHaveValue('');
        expect(box).toHaveFocus();
        fireEvent.keyDown(box, { key: 'Escape' });
        expect(box).not.toHaveFocus();
    });

    it('the kind chips toggle the groups, with their counts under the search', async () => {
        await mount();
        expect(chip('Renderers')).toHaveTextContent('Renderers2');
        fireEvent.click(chip('Renderers'));
        expect(chip('Renderers')).toHaveAttribute('aria-pressed', 'true');
        expect(groupHeads()).toEqual(['Renderers · 2']);
        fireEvent.click(chip('Handlers'));
        expect(groupHeads()).toEqual(['Renderers · 2', 'Handlers · 2']);
        fireEvent.change(search(), { target: { value: 'memo' } });
        expect(chip('Generators')).toHaveTextContent('Generators0');
        expect(chip('Handlers')).toHaveTextContent('Handlers1');
        fireEvent.click(chip('Renderers'));
        fireEvent.click(chip('Handlers'));
        expect(groupHeads()).toEqual(['Renderers · 1 of 2', 'Handlers · 1 of 2']);
    });

    it('a search with no hits offers to create a capability for it, the description prefilled', async () => {
        await mount();
        fireEvent.change(search(), { target: { value: 'spreadsheet report' } });
        expect(screen.getByText('Nothing matches ‘spreadsheet report’.')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Create a capability for it…' }));
        expect(await screen.findByRole('group', { name: 'What should this channel learn to do?' })).toBeInTheDocument();
        fireEvent.click(screen.getByRole('radio', { name: /Not sure/ }));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByRole('textbox', { name: 'What should it do?' })).toHaveValue('spreadsheet report');
        fireEvent.click(screen.getByRole('button', { name: 'Back to Capabilities' }));
        expect(search()).toBeVisible();
    });

    it('a generator row or its cell chip brings its cell to the front; Add runner starts its runner', async () => {
        const { props: p } = await mount();
        const row = document.querySelector('[data-generator-row="synth-gen"]') as HTMLElement;
        fireEvent.click(row);
        expect(p.onShowCell).toHaveBeenCalledWith('tools');
        fireEvent.click(within(row).getByRole('button', { name: 'in Tools' }));
        expect(p.onShowCell).toHaveBeenCalledTimes(2);
        expect(row).toHaveTextContent('one-shot');
        fireEvent.click(within(row).getByRole('button', { name: 'Add runner' }));
        expect(p.onShowCell).toHaveBeenCalledTimes(2);
        expect(await screen.findByRole('group', { name: 'Keep it in' })).toBeInTheDocument();
        expect(screen.getByText('Add a runner of ‘Synth generator’')).toBeInTheDocument();
    });

    it('the problems strip lists what isn’t working, folded, each opening its cell', async () => {
        vi.mocked(fetchCapabilityProblems).mockResolvedValue([
            { cell_id: 'views', path: 'broken/x.renderer', kind: 'renderer', error: 'Claims a built-in extension' },
            { cell_id: 'tools', path: 'bad.generator', kind: 'generator', error: 'No entrypoint' },
        ]);
        const { props: p } = await mount();
        const strip = await screen.findByRole('region', { name: 'Needs attention' });
        const line = within(strip).getByRole('button', { name: '2 files here aren’t working capabilities' });
        expect(line).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(line);
        const rows = within(strip).getAllByRole('listitem');
        expect(rows[0]).toHaveTextContent('broken/x.renderer in Views: Claims a built-in extension');
        fireEvent.click(within(rows[1]).getByRole('button', { name: 'Open cell' }));
        expect(p.onShowCell).toHaveBeenCalledWith('tools');
    });

    it('shows no strip while nothing is broken', async () => {
        await mount();
        expect(screen.queryByRole('region', { name: 'Needs attention' })).toBeNull();
    });

    it('a group that fails says why in an alert, with Retry', async () => {
        vi.mocked(fetchRenderersWithCell).mockRejectedValueOnce(new Error('Server busy'));
        await mount();
        const alert = await screen.findByRole('alert');
        expect(alert).toHaveTextContent('Couldn’t load renderers: Server busy');
        fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(screen.getByRole('region', { name: 'Renderers · 2' })).toBeInTheDocument());
        expect(screen.queryByRole('alert')).toBeNull();
    });

    it('says it is loading until the first catalog lands', async () => {
        vi.mocked(fetchGenerators).mockReturnValue(new Promise(() => {}));
        vi.mocked(fetchRenderersWithCell).mockReturnValue(new Promise(() => {}));
        vi.mocked(fetchHandlers).mockReturnValue(new Promise(() => {}));
        runnerMocks.rows = null;
        render(<CapabilitiesManageCard {...props()} />);
        expect(screen.getByText('Loading capabilities…')).toBeInTheDocument();
    });

    it('reloads on a renderer or handler change and on a change of the stack', async () => {
        await mount();
        expect(fetchHandlers).toHaveBeenCalledTimes(1);
        act(() => { window.dispatchEvent(new Event(RENDERER_PREFS_CHANGED_EVENT)); });
        await waitFor(() => expect(fetchHandlers).toHaveBeenCalledTimes(2));
        expect(fetchGenerators).toHaveBeenCalledTimes(1);
        act(() => { useCollimateStore.setState({ _capabilitiesFingerprint: 'fp2' } as never); });
        await waitFor(() => expect(fetchGenerators).toHaveBeenCalledTimes(2));
        expect(fetchCapabilityProblems).toHaveBeenCalledWith('ch');
    });

    it('owns its bare keys: a ‘/’ typed in its search never reaches the scene’s shortcuts', async () => {
        await mount();
        const owned: boolean[] = [];
        const listen = (e: KeyboardEvent) => owned.push(sceneKeyboardOwned(e));
        window.addEventListener('keydown', listen, true);
        try {
            fireEvent.keyDown(search(), { key: '/' });
        } finally { window.removeEventListener('keydown', listen, true); }
        expect(owned).toEqual([true]);
    });

    it('fronted on the selected stack with the keyboard free, it focuses its search', async () => {
        const view = await mount({ isSelected: false });
        expect(search()).not.toHaveFocus();
        view.rerenderWith({ isSelected: true });
        expect(search()).toHaveFocus();
    });

    it('leaves the keyboard where something else holds it', async () => {
        const other = document.createElement('input');
        document.body.appendChild(other);
        try {
            const view = await mount({ isFront: false });
            other.focus();
            view.rerenderWith({ isFront: true });
            expect(other).toHaveFocus();
        } finally { other.remove(); }
    });
});

describe('Requests from elsewhere (capabilitiesManageRequest)', () => {
    const request = (payload: object) => act(() => { useCollimateStore.getState().requestCapabilitiesManage(payload as never); });

    it('a kind or a query is applied, then the request is cleared', async () => {
        await mount();
        request({ kind: 'renderer', query: 'memo' });
        expect(chip('Renderers')).toHaveAttribute('aria-pressed', 'true');
        expect(search()).toHaveValue('memo');
        await waitFor(() => expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull());
    });

    it('a request waiting when the card mounts is applied on mount', async () => {
        useCollimateStore.getState().requestCapabilitiesManage({ create: true });
        render(<CapabilitiesManageCard {...props()} />);
        expect(await screen.findByRole('group', { name: 'What should this channel learn to do?' })).toBeInTheDocument();
        await waitFor(() => expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull());
    });

    it('a handler focus filters to Handlers and opens the card’s step', async () => {
        await mount();
        fireEvent.change(search(), { target: { value: 'synth' } });
        request({ kind: 'handler', handler: { cellId: 'views', folder: 'memo', step: 'addExample' } });
        expect(search()).toHaveValue('');
        expect(groupHeads()).toEqual(['Handlers · 2']);
        expect(await screen.findByRole('region', { name: 'Add an example to ‘Memo’' })).toBeInTheDocument();
    });

    it('a runner request opens the Keep it in step, or an existing runner’s editor', async () => {
        await mount();
        request({ kind: 'runner', runner: { action: 'add' } });
        expect(await screen.findByRole('group', { name: 'Keep it in' })).toBeInTheDocument();
        vi.mocked(fetchRunner).mockResolvedValue({ runner: { id: 'synth-run', generator: 'synth-gen', name: 'Synth run' }, files: [], revision: 'r1', cell_id: 'tools' } as never);
        request({ runner: { action: 'edit', runnerId: 'synth-run' } });
        expect(await screen.findByTestId('runner-editor')).toHaveTextContent('synth-gen:synth-run');
        expect(fetchRunner).toHaveBeenCalledWith('synth-run');
        expect(screen.getByText('Edit ‘Synth run’')).toBeInTheDocument();
    });

    /** A catalog cell as the channel's catalog read lists it. */
    const catalogCell = (name: string, folder: string, section: 'generator' | 'renderer', held = false) =>
        ({ name, folder, section, held });
    const oneCharts = () => vi.mocked(fetchCapabilityCatalog).mockResolvedValue([
        { id: 'starter', name: 'Starter', description: '', cells: [catalogCell('Charts', 'views/charts', 'renderer')] },
    ]);
    const addCharts = async () => {
        await mount();
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ addFromCatalog: true }); });
        const list = await screen.findByRole('list', { name: 'Catalog cells' });
        expect(within(list).getByRole('listitem')).not.toHaveTextContent('from Starter');
        fireEvent.click(within(list).getByRole('button', { name: 'Add' }));
    };

    it('Add from catalog lists both sections, marks the cells the server says are held, and fronts an added cell', async () => {
        vi.mocked(fetchCapabilityCatalog).mockResolvedValue([
            { id: 'starter', name: 'Starter', description: '', cells: [catalogCell('Tools', 'gens/tools', 'generator', true), catalogCell('Charts', 'views/charts', 'renderer')] },
            { id: 'extra', name: 'Extra', description: '', cells: [catalogCell('Maps', 'views/maps', 'renderer')] },
        ]);
        vi.mocked(addFromCatalog).mockResolvedValue({ cell_id: 'charts-cell', package_skips: [] } as never);
        const { props: p } = await mount();
        fireEvent.click(screen.getByRole('button', { name: 'Add from catalog…' }));
        const list = await screen.findByRole('list', { name: 'Catalog cells' });
        expect(fetchCapabilityCatalog).toHaveBeenCalledWith('ch');
        const rows = within(list).getAllByRole('listitem');
        expect(rows.map(row => row.querySelector('.font-semibold')?.textContent)).toEqual(['Tools', 'Charts', 'Maps']);
        expect(within(rows[0]).getByRole('button', { name: 'Already here' })).toBeDisabled();
        expect(rows[0]).toHaveTextContent('generators');
        expect(rows[1]).toHaveTextContent('renderers and handlers');
        expect(rows[1]).toHaveTextContent('from Starter');
        fireEvent.click(within(rows[1]).getByRole('button', { name: 'Add' }));
        await waitFor(() => expect(p.onShowCell).toHaveBeenCalledWith('charts-cell'));
        expect(addFromCatalog).toHaveBeenCalledWith('ch', { suggestion_id: 'starter', folder: 'views/charts' });
        expect(useCollimateStore.getState().applyOptimisticLayout).toHaveBeenCalledWith({ cell_id: 'charts-cell', package_skips: [] });
        expect(screen.getByRole('status')).toHaveTextContent('‘Charts’ is on Capabilities.');
    });

    it('a board cell named like a catalog cell does not mark it held: only the server’s held flag does', async () => {
        const channel = seedChannel();
        const named = { ...channel, main_stacks: channel.main_stacks.map(stack => (stack.id === 'w' ? { ...stack, cells: [{ id: 'mine', name: 'Charts', files: [] }] } : stack)) };
        useCollimateStore.setState({ activeChannel: named as never });
        oneCharts();
        await mount();
        fireEvent.click(screen.getByRole('button', { name: 'Add from catalog…' }));
        const list = await screen.findByRole('list', { name: 'Catalog cells' });
        expect(within(list).getByRole('button', { name: 'Add' })).toBeEnabled();
        expect(within(list).queryByRole('button', { name: 'Already here' })).toBeNull();
    });

    it('a catalog add the channel already holds says so', async () => {
        oneCharts();
        vi.mocked(addFromCatalog).mockRejectedValue(Object.assign(new Error('Already in this channel'), { response: { status: 409 } }));
        await addCharts();
        expect(await screen.findByRole('alert')).toHaveTextContent('‘Charts’ is already in this channel.');
    });

    it('any other refused catalog add shows the server’s reason', async () => {
        oneCharts();
        const detail = 'Nothing to add: renderer csv-table: .csv is already claimed by Tables';
        vi.mocked(addFromCatalog).mockRejectedValue(Object.assign(new Error(detail), { response: { status: 409 } }));
        await addCharts();
        expect(await screen.findByRole('alert')).toHaveTextContent(detail);
        expect(screen.queryByText(/already in this channel/)).toBeNull();
    });

    it('an editor goes back on Esc', async () => {
        await mount();
        fireEvent.click(screen.getByRole('button', { name: 'Create…' }));
        const step1 = await screen.findByRole('group', { name: 'What should this channel learn to do?' });
        fireEvent.keyDown(step1, { key: 'Escape' });
        expect(screen.queryByRole('group', { name: 'What should this channel learn to do?' })).toBeNull();
        expect(search()).toBeVisible();
    });

    it('paints only from the skin tokens', async () => {
        const { container } = await mount();
        expect(container.innerHTML).not.toMatch(RETIRED_TOKENS);
        expect(cardSource).not.toMatch(/#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?)\(/);
    });
});
