import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HandlerManagement, type HandlerFocus } from './HandlerManagement';
import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    acceptHandler, addBuiltinExample, deleteBuiltinExample, inspectHandler, patchBuiltinExample, revertBuiltinSkills,
    type BuildInspection, type HandlerInfo,
} from '@/lib/definitionApi';
import { spawnHandlerBuild, type SpawnResult } from '@/lib/api/capabilitiesStack';
import { openSpawnedCell } from '@/lib/capabilitySpawn';
import type { Cell } from '@/lib/types';

vi.mock('@/lib/definitionApi', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    inspectHandler: vi.fn(), acceptHandler: vi.fn(async () => ({ accepted: true })),
    addBuiltinExample: vi.fn(async () => ({ folder: 'example-new' })), patchBuiltinExample: vi.fn(async () => ({})),
    deleteBuiltinExample: vi.fn(async () => ({})), revertBuiltinSkills: vi.fn(async () => ({})),
}));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), spawnHandlerBuild: vi.fn() }));
vi.mock('@/lib/capabilitySpawn', () => ({ openSpawnedCell: vi.fn() }));
vi.mock('@/lib/cellSaveBarrier', () => ({ flushCellSaves: vi.fn(async () => {}) }));

const APP = 'a0a0a0a0a0a0a0a0';
const OURS = 'c1c2c3c4c5c6c7c8';
const REGEN = 'e1e2e3e4e5e6e7e8';
const appExample = { folder: 'good', description: 'A complete package', expect: 'valid' as const, feedback: '', added: false };
const added = { folder: 'example-1234abcd', description: 'Mine', expect: 'invalid' as const, feedback: 'Old', added: true };
const builtin = (over: Partial<HandlerInfo> = {}): HandlerInfo => ({
    id: 'sheet', name: 'Sheet', description: 'Checks sheet packages', types: [{ ext: '.sheet' }],
    examples: [appExample, added], cell_id: 'defs', folder: 'sheet', definition_path: 'sheet/sheet.handler',
    has_derived: false, built_hash: null, accepted: true, accepted_derived: APP, disabled: false, clashes: [],
    accepted_skills: ['sheet-authoring'], added_skills: [], builtin: true, accepted_skills_hash: APP, uses_app_skills: true, ...over,
});
const inspection = (over: Partial<BuildInspection> = {}): BuildInspection => ({
    content_hash: 'content-exact-hash', derived_hash: REGEN, compile: { valid: true },
    examples: [{ folder: 'good', expect: 'valid', verdicts: [{ path: 'view.sheet', valid: true }] }], ...over,
});
const SPAWN = { stack_id: 'regen-stack', cell_id: 'regen-cell', brief_path: 'brief.md' } as SpawnResult;

function seed() {
    const file = (name: string, content: string) => fileFixture(name, content, { language: 'yaml' });
    const defs = { id: 'defs', name: 'Definitions', builtin: true, files: [
        file('sheet/sheet.handler', 'id: sheet\n'), file('sheet/examples/good/view.sheet', 'id: v\n'),
        file('sheet/examples/example-1234abcd/broken.sheet', 'id: b\n'),
    ] } as unknown as Cell;
    const work = { id: 'work', name: 'Work', files: [file('pkg/x.sheet', 'id: x\n'), file('notes.txt', 'n')] } as unknown as Cell;
    const channel = { id: 'ch', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: null, main_stacks: [
        { id: 'stack', name: 'Capabilities', stack_type: 'capabilities' as const, cells: [defs] },
        { id: 'w', name: 'Work', stack_type: 'standard' as const, cells: [work] },
    ] };
    useCollimateStore.setState({
        state: { active_channel_id: 'ch', channels: [channel] } as never, activeChannel: channel as never,
        cellCache: {}, cellHistory: {}, pickSessions: {}, cellLayouts: {}, runs: {}, lastFinishedByCell: {},
    } as never);
}

const showCell = vi.fn();
const group = (records: HandlerInfo[], focus: HandlerFocus | null = null, filtering = false) =>
    <HandlerManagement handlers={[]} builtins={records} filtering={filtering} focus={focus} onRevealed={vi.fn()} onEdit={vi.fn()} onShowCell={showCell} />;

/** The app's handlers sit in the group's 'Built-in (n)' disclosure: open it. */
async function mount(records: HandlerInfo[]) {
    const view = render(group(records));
    fireEvent.click(screen.getByRole('button', { name: `Built-in (${records.length})` }));
    await screen.findByRole('article', { name: `${records[0].name} handler` });
    return view;
}
const card = () => screen.getByRole('article', { name: 'Sheet handler' });
const actionNames = () => within(screen.getByRole('group', { name: 'Sheet actions' })).getAllByRole('button').map(button => button.textContent);

beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(inspectHandler).mockResolvedValue(inspection());
    vi.mocked(spawnHandlerBuild).mockResolvedValue(SPAWN);
    seed();
});
afterEach(cleanup);

describe('A built-in handler’s card (the Definitions card)', () => {
    it('is the app’s: a Built-in chip, the app’s checks and skills, and no Edit, Open file or Delete', async () => {
        await mount([builtin()]);
        expect(within(card()).getByText('Built-in')).toHaveAttribute('data-builtin-chip');
        expect(within(card()).getAllByRole('img', { name: 'App’s checks and skills' })[0]).toHaveAttribute('data-status', 'app');
        expect(within(card()).getByText('The app checks every .sheet file and agents get the app’s skills (sheet-authoring).')).toBeInTheDocument();
        expect(actionNames()).toEqual(['Validate', 'Regenerate skills…', 'Add example', 'Open card']);
    });

    it('with this channel’s skills accepted it says so and offers the app’s skills back', async () => {
        await mount([builtin({ accepted_skills_hash: OURS, uses_app_skills: false })]);
        expect(within(card()).getAllByRole('img', { name: 'App’s checks · this channel’s skills' })[0]).toHaveAttribute('data-status', 'channel');
        expect(screen.getByText('The app checks .sheet files. Agents get the skills this channel regenerated (skills c1c2c3c4).')).toBeInTheDocument();
        expect(actionNames()).toEqual(['Validate', 'Regenerate skills…', 'Use the app’s skills', 'Add example', 'Open card']);
        fireEvent.click(screen.getByRole('button', { name: 'Use the app’s skills' }));
        expect(screen.getByText(/^Drop this channel’s regenerated skills of ‘Sheet’\?/).parentElement!.className).toContain('bg-red-soft');
        fireEvent.click(screen.getByRole('button', { name: 'Keep' }));
        expect(revertBuiltinSkills).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', { name: 'Use the app’s skills' }));
        fireEvent.click(screen.getAllByRole('button', { name: 'Use the app’s skills' })[1]);
        await waitFor(() => expect(revertBuiltinSkills).toHaveBeenCalledWith('ch', 'sheet'));
        expect(await within(card()).findByRole('status')).toHaveTextContent('Agents get the app’s skills again.');
    });

    it('a regeneration waiting for review: the status says which skills stay, and Review accepts exactly it', async () => {
        await mount([builtin({ has_derived: true, built_hash: REGEN, accepted: false })]);
        expect(within(card()).getAllByRole('img', { name: 'Regenerated skills waiting for review' })[0].className).toMatch(/\bring-brass\b/);
        expect(screen.getByText('Agents still get the app’s skills until you accept skills e1e2e3e4. The checks never change.')).toBeInTheDocument();
        expect(actionNames()).toEqual(['Validate', 'Regenerate skills…', 'Review and accept', 'Use the app’s skills', 'Add example', 'Open card']);
        fireEvent.click(screen.getByRole('button', { name: 'Review and accept' }));
        const review = await screen.findByRole('region', { name: 'Review skills e1e2e3e4' });
        expect(within(review).getAllByRole('term').map(term => term.textContent)).toEqual(['Status', 'Checks', 'Examples', 'Skills', 'Skills in it']);
        expect(within(review).getByText('The app’s, unchanged')).toBeInTheDocument();
        fireEvent.click(within(review).getByRole('button', { name: 'Accept skills e1e2e3e4' }));
        await waitFor(() => expect(acceptHandler).toHaveBeenCalledWith('ch', 'defs', 'sheet', 'content-exact-hash'));
        expect(await within(card()).findByRole('status')).toHaveTextContent('Skills e1e2e3e4 are in use.');
    });

    it('Regenerate skills… opens a work stack with the projection, then says how the skills come back', async () => {
        await mount([builtin()]);
        fireEvent.click(screen.getByRole('button', { name: 'Regenerate skills…' }));
        const step = screen.getByRole('region', { name: 'Regenerate ‘Sheet’ skills in a work stack' });
        expect(step).toHaveTextContent('Opens a new stack with this built-in handler, this channel’s added examples and its current skills, and a brief for rewriting the skills. Its checks never change. When it’s done, use ‘Use these skills’ on sheet/sheet.handler in that stack’s Files panel; the skills then wait here for review.');
        expect(within(step).queryByRole('list', { name: 'What the work stack holds' })).toBeNull();
        fireEvent.click(within(step).getByRole('button', { name: 'Open work stack' }));
        await waitFor(() => expect(openSpawnedCell).toHaveBeenCalledWith(SPAWN));
        expect(spawnHandlerBuild).toHaveBeenCalledWith('ch', { cell_id: 'defs', folder: 'sheet' });
        expect(await within(card()).findByRole('status')).toHaveTextContent('Work stack ‘Regenerate Sheet skills’ is open.');
    });

    it('Open card brings the Definitions card to the front', async () => {
        await mount([builtin()]);
        fireEvent.click(screen.getByRole('button', { name: 'Open card' }));
        expect(showCell).toHaveBeenCalledWith('defs');
    });

    it('a focus on a built-in unfolds the Built-in disclosure and opens its step', async () => {
        const view = render(group([builtin()]));
        expect(screen.queryByRole('article', { name: 'Sheet handler' })).toBeNull();
        act(() => view.rerender(group([builtin()], { seq: 4, cellId: 'defs', folder: 'sheet', step: 'addExample' })));
        expect(await screen.findByRole('region', { name: 'Add an example to ‘Sheet’' })).toBeInTheDocument();
    });

    it('a search shows the matching built-ins unfolded', () => {
        render(group([builtin()], null, true));
        expect(screen.getByRole('article', { name: 'Sheet handler' })).toBeInTheDocument();
    });

    it('Validate runs every example through the app’s checks: there is no build to compile', async () => {
        await mount([builtin()]);
        const validate = screen.getByRole('button', { name: 'Validate' });
        expect(validate).toBeEnabled();
        fireEvent.click(validate);
        await waitFor(() => expect(inspectHandler).toHaveBeenCalledWith('ch', 'defs', 'sheet'));
        const row = await screen.findByText('good');
        expect(row.closest('[data-example]')).toHaveAttribute('data-verdict', 'match');
    });

    it('the app’s examples are read-only; an added one edits its declaration or goes', async () => {
        await mount([builtin()]);
        fireEvent.click(screen.getByRole('button', { name: /Examples · 2/ }));
        const rows = () => within(screen.getByRole('list', { name: 'Examples' })).getAllByRole('listitem').filter(row => row.hasAttribute('data-example'));
        expect(within(rows()[0]).getByText('Built-in')).toBeInTheDocument();
        expect(within(rows()[0]).queryByRole('textbox')).toBeNull();
        expect(within(rows()[0]).queryByRole('button', { name: 'Remove' })).toBeNull();
        expect(within(rows()[0]).getByRole('button', { name: 'view.sheet' })).toBeInTheDocument();
        expect(within(rows()[1]).getByText('Added')).toBeInTheDocument();
        fireEvent.change(within(rows()[1]).getByRole('textbox', { name: 'Example feedback' }), { target: { value: 'New' } });
        fireEvent.click(within(rows()[1]).getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(patchBuiltinExample).toHaveBeenCalledWith('ch', 'sheet', 'example-1234abcd', { description: 'Mine', expect: 'invalid', feedback: 'New' }));
        fireEvent.click(within(rows()[1]).getByRole('button', { name: 'Remove' }));
        await waitFor(() => expect(deleteBuiltinExample).toHaveBeenCalledWith('ch', 'sheet', 'example-1234abcd'));
    });

    it('Add example takes covered files from any other cell through the host', async () => {
        await mount([builtin()]);
        fireEvent.click(screen.getByRole('button', { name: 'Add example' }));
        const form = screen.getByRole('region', { name: 'Add an example to ‘Sheet’' });
        const from = within(form).getByRole('combobox', { name: 'From cell' });
        expect(within(from).getAllByRole('option').map(option => option.textContent)).toEqual(['Choose a cell', 'Work']);
        fireEvent.change(from, { target: { value: 'work' } });
        fireEvent.click(within(form).getByRole('checkbox', { name: 'pkg/x.sheet' }));
        fireEvent.change(within(form).getByRole('textbox', { name: 'Example feedback' }), { target: { value: 'Name it' } });
        fireEvent.click(within(form).getByRole('button', { name: 'Add example' }));
        await waitFor(() => expect(addBuiltinExample).toHaveBeenCalledWith('ch', 'sheet', {
            source_cell_id: 'work', paths: ['pkg/x.sheet'], description: '', expect: 'valid', feedback: 'Name it',
        }));
        expect(await within(card()).findByRole('status')).toHaveTextContent('Example added.');
    });

    it('past three handlers each card starts folded to name and status; a click unfolds one', async () => {
        const names = ['Sheet', 'Slate', 'Ledger', 'Score', 'Plan'];
        await mount(names.map(name => builtin({ id: name.toLowerCase(), name, folder: name.toLowerCase() })));
        const cards = screen.getAllByRole('article');
        expect(cards.map(item => item.getAttribute('aria-label'))).toEqual(names.map(name => `${name} handler`));
        for (const item of cards) expect(within(item).getByRole('button', { expanded: false })).toBeInTheDocument();
        expect(within(cards[1]).getByText('App’s checks and skills')).toBeInTheDocument();
        expect(screen.queryByRole('group', { name: 'Slate actions' })).toBeNull();
        fireEvent.click(within(cards[1]).getByRole('button', { expanded: false }));
        expect(screen.getByRole('group', { name: 'Slate actions' })).toBeInTheDocument();
    });
});
