import { fileFixture, fixtureText } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { HandlerManagement, type HandlerFocus } from './HandlerManagement';
import { HandlerEditView } from './HandlerEditView';
import cardSource from './HandlerCard.tsx?raw';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { acceptHandler, inspectHandler, type BuildInspection, type HandlerInfo } from '@/lib/definitionApi';
import { RENDERER_PREFS_CHANGED_EVENT } from '@/lib/api';
import { spawnHandlerBuild, type SpawnResult } from '@/lib/api/capabilitiesStack';
import { openSpawnedCell } from '@/lib/capabilitySpawn';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { parseDefinition } from '@/lib/definitionFiles';
import { hint, VALIDATE_NEEDS_BUILD } from '@/lib/handlerStatus';
import type { Cell, VirtualFile } from '@/lib/types';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

vi.mock('@/lib/definitionApi', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    inspectHandler: vi.fn(), acceptHandler: vi.fn(async () => ({ accepted: true })),
}));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), spawnHandlerBuild: vi.fn() }));
vi.mock('@/lib/capabilitySpawn', () => ({ openSpawnedCell: vi.fn() }));
vi.mock('@/lib/cellSaveBarrier', () => ({ flushCellSaves: vi.fn(async () => {}) }));

const BUILT = 'b1b2b3b4b5b6b7b8';
const USED = 'a1a2a3a4a5a6a7a8';
const memoText = [
    'id: memo', 'name: Memo', 'description: A memo with a heading.', 'types:', '  - ext: .memo', 'guidance: Write a heading.',
    'examples:', '  - folder: good', '    description: Good', '    expect: valid', '    feedback: Fine',
    '  - folder: bad', '    description: Bad', '    expect: invalid', '    feedback: Needs a heading', '',
].join('\n');
const handler = (over: Partial<HandlerInfo> = {}): HandlerInfo => ({
    id: 'memo', name: 'Memo', description: 'A memo with a heading.',
    types: [{ ext: '.memo' }, { ext: '.mview', render_as: '.md' }],
    examples: [
        { folder: 'good', description: 'Good', expect: 'valid', feedback: 'Fine' },
        { folder: 'bad', description: 'Bad', expect: 'invalid', feedback: 'Needs a heading' },
    ],
    cell_id: 'cell', folder: 'memo', definition_path: 'memo/memo.handler', has_derived: false, built_hash: null, accepted: false,
    accepted_derived: null, disabled: false, clashes: [], accepted_skills: [], added_skills: [], builtin: false, uses_app_skills: false, ...over,
});
const waiting = () => handler({ has_derived: true, built_hash: BUILT });
const inUse = () => handler({ has_derived: true, built_hash: USED, accepted: true, accepted_derived: USED, accepted_skills: ['memo-writing'] });
const inspection = (over: Partial<BuildInspection> = {}): BuildInspection => ({
    content_hash: 'content-exact-hash', derived_hash: 'd1d2d3d4d5d6d7d8', compile: { valid: true },
    examples: [
        { folder: 'good', expect: 'valid', verdicts: [{ path: 'a.memo', valid: true }] },
        { folder: 'bad', expect: 'invalid', verdicts: [{ path: 'b.memo', valid: true }] },
    ],
    ...over,
});

const file = (name: string, content: string, extra: Partial<VirtualFile> = {}) => fileFixture(name, content, { language: 'yaml', timestamp: 't1', ...extra });
const CELL_FILES = [
    file('memo/memo.handler', memoText),
    file('memo/examples/good/a.memo', '# A'),
    file('memo/examples/bad/b.memo', 'no heading'),
    file('other/view.renderer', 'id: view'),
];
let applied: { cellId: string; files: VirtualFile[] } | null;
const SPAWN = { stack_id: 'build-stack', cell_id: 'build-cell', brief_path: 'brief.md' } as SpawnResult;

function seed() {
    const cell = { id: 'cell', name: 'Views', files: CELL_FILES } as Cell;
    const work = { id: 'work', name: 'Work', files: [file('draft.memo', 'draft bytes'), file('notes.txt', 'notes')] } as Cell;
    const channel = {
        id: 'ch', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: null,
        main_stacks: [
            { id: 'caps', name: 'Capabilities', stack_type: 'capabilities' as const, cells: [cell] },
            { id: 'w', name: 'Work', stack_type: 'standard' as const, cells: [work] },
        ],
    };
    applied = null;
    useCollimateStore.setState({
        state: { active_channel_id: 'ch', channels: [channel] } as never, activeChannel: channel as never,
        cellCache: {}, cellHistory: {}, pickSessions: {}, cellLayouts: {}, runs: {}, lastFinishedByCell: {},
        applyCellFiles: vi.fn(async (cellId: string, files: VirtualFile[]) => { applied = { cellId, files }; return true; }),
    } as never);
}

/** The manage view's Handlers group as the card hosts it: Edit replaces the
 *  list (kept mounted, so the card keeps its notice). */
function Host({ handlers, focus, onShowCell }: { handlers: HandlerInfo[]; focus: HandlerFocus | null; onShowCell: (id: string) => void }) {
    const [editing, setEditing] = useState<{ handler: HandlerInfo; saved: (notice: string) => void } | null>(null);
    return <>
        {editing && <HandlerEditView handler={editing.handler} onBack={() => setEditing(null)} onSaved={notice => { editing.saved(notice); setEditing(null); }} />}
        <div hidden={!!editing}>
            <HandlerManagement handlers={handlers} builtins={[]} filtering={false} focus={focus} onShowCell={onShowCell} onRevealed={revealed}
                onEdit={(handler, saved) => setEditing({ handler, saved })} />
        </div>
    </>;
}
const showCell = vi.fn();
const revealed = vi.fn();

async function mount(record: HandlerInfo, focus: HandlerFocus | null = null) {
    const view = render(<Host handlers={[record]} focus={focus} onShowCell={showCell} />);
    await screen.findByRole('article', { name: 'Memo handler' });
    return { ...view, refocus: (next: HandlerFocus) => view.rerender(<Host handlers={[record]} focus={next} onShowCell={showCell} />) };
}
const theCard = () => screen.getByRole('article', { name: 'Memo handler' });
const actions = () => within(screen.getByRole('group', { name: 'Memo actions' })).getAllByRole('button');
const actionNames = () => actions().map(button => button.textContent);
const step = (name: string) => screen.getByRole('region', { name });

beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(inspectHandler).mockResolvedValue(inspection());
    vi.mocked(spawnHandlerBuild).mockResolvedValue(SPAWN);
    seed();
});
afterEach(cleanup);

describe('A user handler’s card', () => {
    it('says what the handler is: name, id, description and what each type chip means', async () => {
        await mount(handler());
        const card = theCard();
        expect(within(card).getByText('Memo').className).toContain('font-semibold');
        expect(within(card).getByText('memo').className).toMatch(/\bfont-mono\b/);
        expect(within(card).getByText('A memo with a heading.')).toBeInTheDocument();
        expect(within(card).getByText('.memo')).toHaveAttribute('title', 'Files ending .memo');
        expect(within(card).getByText('.mview → shown as .md')).toHaveAttribute('title', 'Opened with the renderer for .md files; saved under its own name');
    });

    it('with no build: No checks yet, Build is the next action, and Validate waits for a build', async () => {
        await mount(handler());
        const card = theCard();
        expect(within(card).getAllByRole('img', { name: 'No checks yet' })[0]).toHaveAttribute('data-status', 'none');
        expect(within(card).getByText(/^Files ending .memo or .mview are saved unchecked/)).toBeInTheDocument();
        expect(within(card).getByText('Next:').textContent).toBe('Next: Build');
        expect(actionNames()).toEqual(['Validate', 'Build…', 'Add example', 'Edit', 'Open file', 'Open cell', 'Delete']);
        const validate = screen.getByRole('button', { name: 'Validate' });
        expect(validate).toBeDisabled();
        expect(validate).toHaveAttribute('title', VALIDATE_NEEDS_BUILD);
        expect(screen.getByRole('button', { name: 'Build…' }).className).toContain('font-semibold');
    });

    it('Open cell brings the handler’s own cell to the front', async () => {
        await mount(handler({ cell_id: 'elsewhere' }));
        fireEvent.click(screen.getByRole('button', { name: 'Open cell' }));
        expect(showCell).toHaveBeenCalledWith('elsewhere');
    });

    it('every action says what it does on hover and to a screen reader', async () => {
        const record = waiting();
        await mount(record);
        const ids = actions().map(button => button.getAttribute('data-action')!);
        expect(ids).toEqual(['validate', 'build', 'review', 'addExample', 'edit', 'openFile', 'openCell', 'delete']);
        for (const button of actions()) {
            const id = button.getAttribute('data-action') as Parameters<typeof hint>[0];
            expect(button).toHaveAttribute('title', hint(id, record));
            expect(button).toHaveAttribute('aria-description', hint(id, record));
        }
    });

    it('offers Review and accept only while a build waits, as the primary action', async () => {
        await mount(waiting());
        expect(within(theCard()).getAllByRole('img', { name: 'Build waiting for review' })[0]).toHaveAttribute('data-status', 'review-first');
        expect(screen.getByRole('button', { name: 'Review and accept' }).className).toContain('font-semibold');
        expect(screen.getByText('Next:').textContent).toBe('Next: Review and accept');
        cleanup();
        await mount(inUse());
        expect(within(theCard()).getAllByRole('img', { name: 'In use' })[0].className).toMatch(/\bbg-green\b/);
        expect(screen.queryByRole('button', { name: 'Review and accept' })).toBeNull();
        expect(screen.getByText('Next:').textContent).toBe('Next: —');
        cleanup();
        await mount(handler({ has_derived: true, built_hash: BUILT, accepted_derived: USED }));
        expect(within(theCard()).getAllByRole('img', { name: 'In use · new build waiting' })[0].className).toMatch(/\bring-brass\b/);
        expect(screen.getByText('Build a1a2a3a4 stays in use until you accept build b1b2b3b4.')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Review and accept' })).toBeInTheDocument();
    });

    it('a clash shows as a red line under the status', async () => {
        await mount(handler({ clashes: ['.memo'] }));
        expect(screen.getByText('Shares .memo with another handler, so neither handles .memo files.').className).toMatch(/\btext-red\b/);
    });

    it('Validate puts each example’s verdict on its row and opens the examples', async () => {
        await mount(waiting());
        fireEvent.click(screen.getByRole('button', { name: 'Validate' }));
        await waitFor(() => expect(inspectHandler).toHaveBeenCalledWith('ch', 'cell', 'memo'));
        const list = await screen.findByRole('list', { name: 'Examples' });
        const rows = within(list).getAllByRole('listitem').filter(row => row.hasAttribute('data-example'));
        expect(rows.map(row => row.getAttribute('data-verdict'))).toEqual(['match', 'mismatch']);
        expect(within(rows[0]).getByRole('img', { name: 'Matches' }).className).toMatch(/\bbg-green\b/);
        expect(within(rows[1]).getByRole('img', { name: 'Mismatch' }).className).toMatch(/\bbg-red\b/);
        expect(rows[1]).toHaveTextContent('Mismatch: expected invalid, got valid');
        expect(step('Validate ‘Memo’')).toHaveTextContent('1 of 2 examples match what they expect.');
        expect(screen.getByRole('button', { name: /Examples · 2/ })).toHaveTextContent('1 of 2 match');
    });

    it('Validate says plainly when the build’s checks do not compile', async () => {
        vi.mocked(inspectHandler).mockResolvedValueOnce(inspection({ compile: { valid: false, error: 'Cannot import verify.ts' } }));
        await mount(waiting());
        fireEvent.click(screen.getByRole('button', { name: 'Validate' }));
        const alert = await within(step('Validate ‘Memo’')).findByRole('alert');
        expect(alert).toHaveTextContent('The checks of build b1b2b3b4 do not compile: Cannot import verify.ts');
        expect(alert.className).toMatch(/\btext-red\b/);
    });

    it('Review and accept shows the waiting build and accepts exactly what it inspected', async () => {
        const announced = vi.fn();
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, announced);
        try {
            await mount(waiting());
            fireEvent.click(screen.getByRole('button', { name: 'Review and accept' }));
            const review = await screen.findByRole('region', { name: 'Review build d1d2d3d4' });
            expect(within(review).getAllByRole('term').map(term => term.textContent)).toEqual(['Status', 'Checks', 'Examples', 'Build', 'Skills in it']);
            expect(within(review).getByText('Build waiting for review')).toBeInTheDocument();
            expect(within(review).getByText('Compile')).toBeInTheDocument();
            expect(within(review).getByText('d1d2d3d4')).toHaveAttribute('title', 'd1d2d3d4d5d6d7d8');
            fireEvent.click(within(review).getByRole('button', { name: '1 of 2 match' }));
            expect(within(review).getAllByRole('listitem').map(item => item.getAttribute('data-outcome'))).toEqual(['pass', 'fail']);
            fireEvent.click(within(review).getByRole('button', { name: 'Accept build d1d2d3d4' }));
            await waitFor(() => expect(acceptHandler).toHaveBeenCalledWith('ch', 'cell', 'memo', 'content-exact-hash'));
            expect(await within(theCard()).findByRole('status')).toHaveTextContent('Build d1d2d3d4 is in use.');
            expect(announced).toHaveBeenCalled();
        } finally { window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, announced); }
    });

    it('Review keeps Accept off while the checks do not compile, and a changed handler asks for a new review', async () => {
        vi.mocked(inspectHandler).mockResolvedValueOnce(inspection({ compile: { valid: false, error: 'Cannot import verify.ts' } }));
        await mount(waiting());
        fireEvent.click(screen.getByRole('button', { name: 'Review and accept' }));
        let review = await screen.findByRole('region', { name: 'Review build d1d2d3d4' });
        expect(within(review).getByText('Cannot import verify.ts').className).toMatch(/\btext-red\b/);
        expect(within(review).getByRole('button', { name: 'Accept build d1d2d3d4' })).toBeDisabled();
        vi.mocked(acceptHandler).mockRejectedValueOnce(Object.assign(new Error('Request failed (409)'), { response: { status: 409 } }));
        fireEvent.click(screen.getByRole('button', { name: 'Review and accept' }));
        await waitFor(() => expect(within(screen.getByRole('region', { name: 'Review build d1d2d3d4' })).getByText('Compile')).toBeInTheDocument());
        review = screen.getByRole('region', { name: 'Review build d1d2d3d4' });
        fireEvent.click(within(review).getByRole('button', { name: 'Accept build d1d2d3d4' }));
        expect(await within(theCard()).findByRole('alert')).toHaveTextContent('The handler changed while you reviewed it. Review again.');
    });

    it('Add example copies covered files from another cell into its examples folder and declares them', async () => {
        await mount(inUse());
        fireEvent.click(screen.getByRole('button', { name: 'Add example' }));
        const form = step('Add an example to ‘Memo’');
        const from = within(form).getByRole('combobox', { name: 'From cell' });
        // Never the handler's own files, never a file it does not cover.
        expect(within(from).getAllByRole('option').map(option => option.textContent)).toEqual(['Choose a cell', 'Work']);
        fireEvent.change(from, { target: { value: 'work' } });
        expect(within(form).queryByRole('checkbox', { name: 'notes.txt' })).toBeNull();
        fireEvent.click(within(form).getByRole('checkbox', { name: 'draft.memo' }));
        fireEvent.change(within(form).getByRole('textbox', { name: 'Example description' }), { target: { value: 'A draft' } });
        fireEvent.change(within(form).getByRole('combobox', { name: 'Expected verdict' }), { target: { value: 'invalid' } });
        fireEvent.click(within(form).getByRole('button', { name: 'Add example' }));
        await waitFor(() => expect(applied).not.toBeNull());
        expect(applied!.cellId).toBe('cell');
        const copy = applied!.files.find(item => /^memo\/examples\/example-[0-9a-f]{8}\/draft\.memo$/.test(item.name));
        expect(copy).toMatchObject({ content: 'draft bytes' });
        const folder = copy!.name.split('/')[2];
        const manifest = parseDefinition(fixtureText(applied!.files.find(item => item.name === 'memo/memo.handler')));
        expect((manifest.examples as { folder: string }[]).at(-1)).toEqual({ folder, description: 'A draft', expect: 'invalid', feedback: '' });
        expect(applied!.files.find(item => item.name === 'other/view.renderer')).toBe(CELL_FILES[3]);
        expect(await within(theCard()).findByRole('status')).toHaveTextContent('Example added.');
    });

    it('edits an example in place and removes one with its files', async () => {
        await mount(inUse());
        fireEvent.click(screen.getByRole('button', { name: /Examples · 2/ }));
        const rows = () => within(screen.getByRole('list', { name: 'Examples' })).getAllByRole('listitem').filter(row => row.hasAttribute('data-example'));
        fireEvent.change(within(rows()[0]).getByRole('textbox', { name: 'Example feedback' }), { target: { value: 'Nice heading' } });
        fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(applied).not.toBeNull());
        let manifest = parseDefinition(fixtureText(applied!.files.find(item => item.name === 'memo/memo.handler')));
        expect((manifest.examples as { feedback: string }[])[0].feedback).toBe('Nice heading');
        applied = null;
        fireEvent.click(within(rows()[1]).getByRole('button', { name: 'Remove' }));
        await waitFor(() => expect(applied).not.toBeNull());
        manifest = parseDefinition(fixtureText(applied!.files.find(item => item.name === 'memo/memo.handler')));
        expect((manifest.examples as { folder: string }[]).map(example => example.folder)).toEqual(['good']);
        expect(applied!.files.map(item => item.name)).not.toContain('memo/examples/bad/b.memo');
        expect(applied!.files.map(item => item.name)).toContain('memo/examples/good/a.memo');
    });

    it('Delete confirms in a strip, then removes only the handler’s folder from its cell', async () => {
        const announced = vi.fn();
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, announced);
        try {
            await mount(inUse());
            fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
            const strip = screen.getByText(/^Delete ‘Memo’\?/);
            expect(strip.parentElement!.className).toContain('bg-red-soft');
            fireEvent.click(screen.getByRole('button', { name: 'Keep' }));
            expect(applied).toBeNull();
            fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
            fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[1]);
            await waitFor(() => expect(applied).not.toBeNull());
            expect(flushCellSaves).toHaveBeenCalledWith('cell');
            expect(applied!.cellId).toBe('cell');
            expect(applied!.files.map(item => item.name)).toEqual(['other/view.renderer']);
            expect(announced).toHaveBeenCalled();
        } finally { window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, announced); }
    });

    it('Edit replaces the list with the definition form; Save is revision-checked and says when to build again', async () => {
        await mount(inUse());
        fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
        const guidance = await screen.findByRole('textbox', { name: 'guidance' });
        expect(screen.queryByRole('article', { name: 'Memo handler' })).toBeNull();
        expect(screen.getByText('Edit ‘Memo’')).toBeInTheDocument();
        // Someone else saved the definition: the save is refused.
        useCollimateStore.setState({ cellCache: { cell: { id: 'cell', name: 'Views', files: [file('memo/memo.handler', memoText, { file_revision: 2 }), ...CELL_FILES.slice(1)] } } as never });
        fireEvent.change(guidance, { target: { value: 'Write two headings.' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save definition' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('The handler definition changed');
        expect(applied).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Back to Capabilities' }));
        expect(theCard()).toBeInTheDocument();
        useCollimateStore.setState({ cellCache: {} } as never);
        fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
        fireEvent.change(await screen.findByRole('textbox', { name: 'guidance' }), { target: { value: 'Write two headings.' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save definition' }));
        await waitFor(() => expect(applied).not.toBeNull());
        expect(parseDefinition(fixtureText(applied!.files.find(item => item.name === 'memo/memo.handler'))).guidance).toBe('Write two headings.');
        expect(await within(theCard()).findByRole('status')).toHaveTextContent('Saved. Build again to use new guidance.');
    });

    it('Build… explains the work stack, then opens it from the saved cell and says how the build comes back', async () => {
        await mount(handler({ has_derived: true, built_hash: BUILT, added_skills: ['house-style'] }));
        fireEvent.click(screen.getByRole('button', { name: 'Build…' }));
        const build = step('Build ‘Memo’ in a work stack');
        expect(build).toHaveTextContent('Opens a new stack beside Capabilities with a copy of this handler and a brief for writing its checks (derived/verify.ts) and agent skills (derived/skills/). Run any runner there. When it’s done, add memo/ back from that stack’s Files panel. The new build then waits here for your review; the build in use stays in use.');
        expect(within(build).getByText('Copies memo/ (2 examples, current build b1b2b3b4) · brief.md · guides/')).toBeInTheDocument();
        expect(within(build).getByText('Stays here: added skills (skills/)')).toBeInTheDocument();
        expect(within(build).getByText('Runners that start from the open file follow brief.md; with others, tell it to follow brief.md.')).toBeInTheDocument();
        fireEvent.click(within(build).getByRole('button', { name: 'Cancel' }));
        expect(screen.queryByRole('region', { name: 'Build ‘Memo’ in a work stack' })).toBeNull();
        expect(spawnHandlerBuild).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', { name: 'Build…' }));
        fireEvent.click(within(step('Build ‘Memo’ in a work stack')).getByRole('button', { name: 'Open work stack' }));
        await waitFor(() => expect(openSpawnedCell).toHaveBeenCalledWith(SPAWN));
        expect(flushCellSaves).toHaveBeenCalledWith('cell');
        expect(vi.mocked(flushCellSaves).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(spawnHandlerBuild).mock.invocationCallOrder[0]);
        expect(spawnHandlerBuild).toHaveBeenCalledWith('ch', { cell_id: 'cell', folder: 'memo' });
        expect(await within(theCard()).findByRole('status')).toHaveTextContent('Work stack ‘Build Memo’ is open. Add memo/ back from its Files panel, then review the build here.');
        expect(screen.queryByRole('region', { name: 'Build ‘Memo’ in a work stack' })).toBeNull();
    });

    it('a handler with no build and no added skills says so in the facts', async () => {
        await mount(handler());
        fireEvent.click(screen.getByRole('button', { name: 'Build…' }));
        const build = step('Build ‘Memo’ in a work stack');
        expect(within(build).getByText('Copies memo/ (2 examples, current build none) · brief.md · guides/')).toBeInTheDocument();
        expect(within(build).queryByText('Stays here: added skills (skills/)')).toBeNull();
    });

    it('a failed spawn stays on the card as an alert', async () => {
        vi.mocked(spawnHandlerBuild).mockRejectedValueOnce(new Error('Handler declaration is unavailable or invalid'));
        await mount(handler());
        fireEvent.click(screen.getByRole('button', { name: 'Build…' }));
        fireEvent.click(within(step('Build ‘Memo’ in a work stack')).getByRole('button', { name: 'Open work stack' }));
        expect(await within(theCard()).findByRole('alert')).toHaveTextContent('Handler declaration is unavailable or invalid');
        expect(openSpawnedCell).not.toHaveBeenCalled();
    });

    it('a focus unfolds the card, flashes it and opens its step, once per seq', async () => {
        const view = await mount(handler());
        act(() => view.refocus({ seq: 1, cellId: 'cell', folder: 'memo', step: 'addExample' }));
        expect(await screen.findByRole('region', { name: 'Add an example to ‘Memo’' })).toBeInTheDocument();
        expect(theCard().className).toMatch(/\bborder-teal\b/);
        expect(revealed).toHaveBeenCalledWith(1);
        fireEvent.click(within(step('Add an example to ‘Memo’')).getByRole('button', { name: 'Close' }));
        act(() => view.refocus({ seq: 1, cellId: 'cell', folder: 'memo', step: 'addExample' }));
        expect(screen.queryByRole('region', { name: 'Add an example to ‘Memo’' })).toBeNull();
        expect(revealed).toHaveBeenCalledTimes(1);
    });

    it('a focus on another cell’s handler of the same folder leaves this card alone', async () => {
        const view = await mount(handler());
        act(() => view.refocus({ seq: 1, cellId: 'other', folder: 'memo', step: 'addExample' }));
        expect(screen.queryByRole('region', { name: 'Add an example to ‘Memo’' })).toBeNull();
        expect(revealed).not.toHaveBeenCalled();
    });

    it('a focus its holder drops once shown still runs the inspection it asked for', async () => {
        function Dropping({ record }: { record: HandlerInfo }) {
            const [focus, setFocus] = useState<HandlerFocus | null>({ seq: 1, cellId: 'cell', folder: 'memo', step: 'review' });
            return <HandlerManagement handlers={[record]} builtins={[]} filtering={false} focus={focus} onShowCell={showCell}
                onRevealed={seq => setFocus(current => (current?.seq === seq ? null : current))} onEdit={vi.fn()} />;
        }
        render(<Dropping record={waiting()} />);
        expect(await screen.findByRole('region', { name: 'Review build d1d2d3d4' })).toBeInTheDocument();
        expect(inspectHandler).toHaveBeenCalledTimes(1);
    });

    it('a focus on Review inspects the waiting build and opens its review', async () => {
        const view = await mount(waiting());
        act(() => view.refocus({ seq: 1, cellId: 'cell', folder: 'memo', step: 'review' }));
        expect(await screen.findByRole('region', { name: 'Review build d1d2d3d4' })).toBeInTheDocument();
        expect(inspectHandler).toHaveBeenCalledWith('ch', 'cell', 'memo');
    });

    it('paints only from the skin tokens', async () => {
        const view = await mount(waiting());
        expect(view.container.innerHTML).not.toMatch(RETIRED_TOKENS);
        expect(cardSource).not.toMatch(RETIRED_TOKENS);
        expect(cardSource).not.toMatch(/#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?)\(/);
    });
});
