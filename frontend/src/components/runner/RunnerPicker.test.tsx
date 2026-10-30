import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { RunnerPicker } from './RunnerPicker';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { runRunner } from '@/lib/runAttach';
import type { RunnerEntry } from '@/lib/runnerApi';
import type { RunRecord } from '@/lib/types';
import { createDefaultLayout, openFileTab } from '@/lib/cellLayout';

const mocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[], busy: false }));
vi.mock('@/stores/useRunnerStore', () => ({ useRunnerCatalog: () => ({ data: mocks.rows, busy: mocks.busy, error: null, refresh: vi.fn() }) }));
vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/lib/runAttach', () => ({ runRunner: vi.fn(async () => {}) }));

const runner: RunnerEntry = { id: 'writer', generator: 'gen', name: 'Writer', description: 'Edit this cell', tags: ['writing'], prompt: 'Read cell', settings: {}, accounts: {}, folders: [], permissions: {}, packages: {}, revision: 'original', cell_id: 'source' };
const second: RunnerEntry = { ...runner, id: 'summarize', name: 'Summarize', description: 'Shorten it', tags: [], revision: 'second' };
const liveRun = (over: Partial<RunRecord> = {}): RunRecord => ({ runId: 'run-1', cellId: 'target', channel: 'ch', runnerId: 'summarize', runnerName: 'Summarize', generatorName: 'g', label: 'summarize-run-1', folder: '_runs/summarize-run-1', ...over });

const search = () => screen.getByRole('combobox', { name: 'Search runners' });
const options = () => screen.getAllByRole('option');
const names = () => options().map(o => o.querySelector('div')!.textContent);
const selectedName = () => options().find(o => o.getAttribute('aria-selected') === 'true')?.querySelector('div')?.textContent;
const primary = () => screen.getByTestId('cell-panel-primary');
const secondary = () => screen.queryByTestId('cell-panel-secondary');

beforeEach(() => {
    vi.clearAllMocks(); mocks.rows = [runner, second]; mocks.busy = false;
    useCollimateStore.setState({ pickSessions: { target: { kind: 'runners', query: '', at: 1 } }, runs: {}, lastFinishedByCell: {}, cellLayouts: {}, capabilitiesManageRequest: null });
});
afterEach(cleanup);

describe('Runners panel', () => {
    it('is the cell panel titled Runners with the collision note and the search, and runs only (P02)', () => {
        render(<RunnerPicker cellId="target" />);
        const panel = screen.getByRole('dialog', { name: 'Runners' });
        expect(panel).toHaveAttribute('data-cell-panel', 'runners');
        expect(panel).toHaveAttribute('data-pick-cell', 'target');
        expect(screen.getByTestId('cell-panel-note').textContent).toBe('Several runners in one cell can collide on the files they work on. Run in a new cell to keep them apart.');
        expect(screen.queryByRole('group', { name: 'Mode' })).toBeNull();
        expect(document.activeElement).toBe(search());
        expect(search().closest('label')!.querySelector('.lucide-search')).not.toBeNull();
        expect(screen.getByRole('listbox', { name: 'Runners' })).toBeTruthy();
        expect(names()).toEqual(['Writer', 'Summarize']);
        expect(primary().textContent).toBe('Run ‘Writer’↵');
        expect(primary().querySelector('[data-play-glyph]')).not.toBeNull();
        expect(primary().className).toMatch(/\[&_svg\]:text-brass/);
        expect(screen.queryByRole('button', { name: /Edit|Add|Duplicate|Delete/ })).toBeNull();
        // Tab changes nothing: there is no other mode.
        fireEvent.keyDown(search(), { key: 'Tab' });
        expect(primary().textContent).toBe('Run ‘Writer’↵');
    });

    it('Enter runs the selected runner in this cell and closes the panel (D-P02a) (P02)', async () => {
        render(<RunnerPicker cellId="target" />);
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        expect(selectedName()).toBe('Summarize');
        fireEvent.keyDown(search(), { key: 'Enter' });
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({ runner: 'summarize', runnerRevision: 'second', cellId: 'target' }));
        await waitFor(() => expect(useCollimateStore.getState().pickSessions.target).toBeUndefined());
    });

    it('Shift+Enter and the "in new cell ⇧↵" button run it in a new cell (D-P02a) (P02)', async () => {
        render(<RunnerPicker cellId="target" />);
        expect(secondary()!.textContent).toBe('in new cell⇧↵');
        fireEvent.keyDown(search(), { key: 'Enter', shiftKey: true });
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({ runner: 'writer', runnerRevision: 'original', cellId: 'target', newCell: true }));
        cleanup();
        vi.mocked(runRunner).mockClear();
        useCollimateStore.setState({ pickSessions: { target: { kind: 'runners', query: '', at: 1 } } });
        render(<RunnerPicker cellId="target" />);
        fireEvent.click(secondary()!);
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({ runner: 'writer', runnerRevision: 'original', cellId: 'target', newCell: true }));
    });

    it('a click selects a runner; a double-click runs it (P02)', async () => {
        render(<RunnerPicker cellId="target" />);
        fireEvent.click(options()[1]);
        expect(selectedName()).toBe('Summarize');
        expect(runRunner).not.toHaveBeenCalled();
        fireEvent.doubleClick(options()[1]);
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({ runner: 'summarize', runnerRevision: 'second', cellId: 'target' }));
    });

    it('opens on the cell\'s last runner: its live run\'s, else its last finished run\'s (P02)', () => {
        useCollimateStore.setState({ runs: { 'run-1': liveRun() } });
        const view = render(<RunnerPicker cellId="target" />);
        expect(selectedName()).toBe('Summarize');
        expect(primary().textContent).toBe('Run ‘Summarize’↵');
        view.unmount();
        useCollimateStore.setState({
            runs: {},
            lastFinishedByCell: { target: { runId: 'abcdef12-3456', label: 'summarize-abcdef12', status: 'completed', seen: true, at: 1 } },
        });
        const again = render(<RunnerPicker cellId="target" />);
        expect(selectedName()).toBe('Summarize');
        again.unmount();
        // A runner-less run names no runner: the top row stays selected.
        useCollimateStore.setState({ lastFinishedByCell: { target: { runId: 'abcdef12-3456', label: 'run-abcdef12', status: 'completed', seen: true, at: 1 } } });
        render(<RunnerPicker cellId="target" />);
        expect(selectedName()).toBe('Writer');
    });

    it('the search persists to the cell\'s session and selects its best match (P02)', () => {
        useCollimateStore.setState({ runs: { 'run-1': liveRun() } });
        const todo: RunnerEntry = { ...runner, id: 'todo', name: 'Todo agent', description: 'Keeps a todo list next to its chat', tags: ['agent'], revision: 'todo-rev' };
        const chat: RunnerEntry = { ...runner, id: 'chat', name: 'Chat agent', description: 'Talk it through', tags: [], revision: 'chat-rev' };
        mocks.rows = [todo, chat, second];
        render(<RunnerPicker cellId="target" />);
        expect(selectedName()).toBe('Summarize');
        fireEvent.change(search(), { target: { value: 'Chat agent' } });
        expect(useCollimateStore.getState().pickSessions.target).toMatchObject({ kind: 'runners', query: 'Chat agent' });
        expect(names()).toEqual(['Chat agent', 'Todo agent']);
        expect(selectedName()).toBe('Chat agent');
        expect(screen.getByText('Talk it through')).toBeTruthy();
    });

    it('ranks an exact name, then a name prefix, then a name holding the words, then the rest (P02)', () => {
        const todo: RunnerEntry = { ...runner, id: 'todo', name: 'Todo agent', description: 'chat', tags: [] };
        const contains: RunnerEntry = { ...runner, id: 'my-chat-agent', name: 'My chat agent', description: '', tags: [] };
        const prefix: RunnerEntry = { ...runner, id: 'chat-agent-pro', name: 'Chat Agent Pro', description: '', tags: [] };
        const exact: RunnerEntry = { ...runner, id: 'chat-agent', name: 'Chat  agent', description: '', tags: [] };
        mocks.rows = [todo, contains, prefix, exact, second];
        render(<RunnerPicker cellId="target" />);
        fireEvent.change(search(), { target: { value: '  chat AGENT ' } });
        expect(names()).toEqual(['Chat  agent', 'Chat Agent Pro', 'My chat agent', 'Todo agent']);
        fireEvent.change(search(), { target: { value: 'nothing' } });
        expect(screen.getByText('No runner matches')).toBeTruthy();
        expect(primary()).toBeDisabled();
    });

    it('a runner that cannot run shows why and offers no Run (P02)', () => {
        mocks.rows = [{ ...runner, invalid: { reason: 'Select an account' } }];
        render(<RunnerPicker cellId="target" />);
        expect(screen.getByText('Select an account').className).toMatch(/\btext-red\b/);
        expect(primary()).toBeDisabled();
        expect(primary()).toHaveAttribute('title', 'Select an account');
        expect(secondary()).toBeDisabled();
        fireEvent.keyDown(search(), { key: 'Enter' });
        fireEvent.keyDown(search(), { key: 'Enter', shiftKey: true });
        expect(runRunner).not.toHaveBeenCalled();
    });

    it('with no runners it says where they are made and its Open Capabilities asks for the manage view\'s Add runner, then closes', () => {
        mocks.rows = [];
        render(<RunnerPicker cellId="target" />);
        expect(screen.getByText('No runners yet. Make one in Capabilities.')).toBeTruthy();
        expect(primary()).toBeDisabled();
        fireEvent.click(screen.getByRole('button', { name: 'Open Capabilities' }));
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ kind: 'runner', runner: { action: 'add' } });
        expect(useCollimateStore.getState().pickSessions.target).toBeUndefined();
    });

    it('offers Open Capabilities only for an empty catalog, never for a search with no match or while loading', () => {
        render(<RunnerPicker cellId="target" />);
        fireEvent.change(search(), { target: { value: 'nothing' } });
        expect(screen.getByText('No runner matches')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Open Capabilities' })).toBeNull();
        cleanup();
        mocks.rows = []; mocks.busy = true;
        useCollimateStore.setState({ pickSessions: { target: { kind: 'runners', query: '', at: 1 } } });
        render(<RunnerPicker cellId="target" />);
        expect(screen.getByText('Loading runners…')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Open Capabilities' })).toBeNull();
    });

    it('the collision note turns teal while runs are live on the cell (P02)', () => {
        const view = render(<RunnerPicker cellId="target" />);
        expect(screen.getByTestId('cell-panel-note').querySelector('.text-teal')).toBeNull();
        view.unmount();
        useCollimateStore.setState({ runs: { 'run-1': liveRun({ runnerId: 'other' }) } });
        render(<RunnerPicker cellId="target" />);
        expect(screen.getByTestId('cell-panel-note').querySelector('.text-teal')).not.toBeNull();
    });

    it('a failed start is reported and the panel stays (P02)', async () => {
        vi.mocked(runRunner).mockRejectedValueOnce(new Error('Cannot run — required fields are empty: Prompt'));
        render(<RunnerPicker cellId="target" />);
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect((await screen.findByRole('alert')).textContent).toBe('Cannot run — required fields are empty: Prompt');
        expect(useCollimateStore.getState().pickSessions.target).toBeDefined();
    });

    it('Close and Esc close the cell\'s runners session (P02)', () => {
        render(<RunnerPicker cellId="target" />);
        fireEvent.keyDown(search(), { key: 'Escape' });
        expect(useCollimateStore.getState().pickSessions.target).toBeUndefined();
        cleanup();
        useCollimateStore.setState({ pickSessions: { target: { kind: 'runners', query: '', at: 1 } } });
        render(<RunnerPicker cellId="target" />);
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(useCollimateStore.getState().pickSessions.target).toBeUndefined();
    });
});

describe('Runners panel — the selected file (GN-01)', () => {
    const taking: RunnerEntry = { ...runner, takes_selected_file: true };
    const openFile = (path: string) => useCollimateStore.setState({
        cellLayouts: { target: openFileTab(createDefaultLayout({ filePaths: [], pinnedPath: null }), path, null) },
    });
    const notes = () => options().map(o => within(o).queryByTestId('cell-panel-row-note')?.textContent ?? null);

    it('a runner that takes the selected file says which one it will use; others say nothing (GN-01)', () => {
        mocks.rows = [taking, second];
        openFile('docs/brief.md');
        render(<RunnerPicker cellId="target" />);
        expect(notes()).toEqual(['will use brief.md', null]);
    });

    it('with no file selected the note says so and the run carries none (GN-01)', async () => {
        mocks.rows = [taking, second];
        render(<RunnerPicker cellId="target" />);
        expect(notes()).toEqual(['no file selected', null]);
        fireEvent.keyDown(search(), { key: 'Enter' });
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({ runner: 'writer', runnerRevision: 'original', cellId: 'target' }));
    });

    it('Run and Run in new cell pass the selected file; a runner that takes none gets none (GN-01)', async () => {
        mocks.rows = [taking, second];
        openFile('docs/brief.md');
        render(<RunnerPicker cellId="target" />);
        fireEvent.keyDown(search(), { key: 'Enter', shiftKey: true });
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({
            runner: 'writer', runnerRevision: 'original', cellId: 'target', newCell: true, selectedFile: 'docs/brief.md' }));
        cleanup();
        vi.mocked(runRunner).mockClear();
        useCollimateStore.setState({ pickSessions: { target: { kind: 'runners', query: '', at: 1 } } });
        render(<RunnerPicker cellId="target" />);
        fireEvent.keyDown(search(), { key: 'Enter' });
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({
            runner: 'writer', runnerRevision: 'original', cellId: 'target', selectedFile: 'docs/brief.md' }));
        cleanup();
        vi.mocked(runRunner).mockClear();
        useCollimateStore.setState({ pickSessions: { target: { kind: 'runners', query: '', at: 1 } } });
        render(<RunnerPicker cellId="target" />);
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        fireEvent.keyDown(search(), { key: 'Enter' });
        await waitFor(() => expect(runRunner).toHaveBeenCalledExactlyOnceWith({ runner: 'summarize', runnerRevision: 'second', cellId: 'target' }));
    });
});
