import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import { createPortal } from 'react-dom';
import { CommandPane, type CommandPaneScene } from './CommandPane';
import { paneHostsPanel } from '@/lib/commandPane';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { defaultFilesPane, defaultRunsPane } from '@/lib/cellLayout';
import { useKeyboardHandler } from '@/hooks/useKeyboardHandler';
import { RunStatusIndicator } from './RunStatusIndicator';
import { EditableLabel } from './ui/editable-label';
import type { CellLayout } from '@/stores/collimate/types';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { FocusContextType } from '@/contexts/FocusContext';
import type { Cell, Stack } from '@/lib/types';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import { allShortcuts, SHORTCUT_REGISTRY } from '@/lib/keybindings/registry';
import { displayComboString } from '@/lib/keybindings/display';
import { SHORTCUT_CONTEXT_LABEL } from '@/lib/keybindings/types';

// The settings sections as the pane loads them (lazily, on first open).
vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: () => <div>keys body</div> }));
vi.mock('@/components/settings/EditingSection', () => ({ EditingSection: () => <input aria-label="Editing field" /> }));
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: () => <div>theme body</div> }));
vi.mock('@/components/settings/ServersSection', () => ({ ServersSection: () => <div>servers body</div> }));

const cell = (id: string, name: string, files: string[] = []): Cell => ({
    id, name, files: files.map(f => (fileFixture(f, '', {language: 'text'}))),
} as Cell);
const frame: Stack = { id: 's1', name: 'Frame', cells: [cell('a', 'Offline editing', ['notes/a.md', 'b.options_stack']), cell('b', 'Second'), cell('c', 'Third')] };
const options: Stack = { id: 's2', name: 'Options', cells: [cell('d', 'CRDT (Yjs)')] };
// Capabilities leads with its manage card (slot 0); its cell `r` is slot 1.
const capabilitiesStack: Stack = { id: 's3', name: 'Capabilities', stack_type: 'capabilities', cells: [cell('r', 'Kit')] };
const STACKS = [frame, options, capabilitiesStack];
/** The scene with the Capabilities cell `r` selected (its slot in front). */
const onCapabilitiesCell = { displaySelectedId: 's3', selectedStackId: 's3', visibleTopIndices: { s3: 1 } };

const layout = (): CellLayout => ({
    root: {
        type: 'leaf', id: 'g', activeTabId: 't1',
        tabs: [{ id: 't1', kind: 'file', path: 'notes/a.md' }, { id: 't2', kind: 'file', path: 'b.options_stack' }],
    },
    activeLeafId: 'g',
    filesPane: { ...defaultFilesPane(), collapsed: false },
    runsPane: defaultRunsPane(),
} as CellLayout);

const makeActions = () => ({
    winRotate: vi.fn(), splitCell: vi.fn(async () => {}), newEmptyStackRight: vi.fn(async () => null),
}) as unknown as SceneActions & Record<'winRotate' | 'splitCell' | 'newEmptyStackRight', ReturnType<typeof vi.fn>>;

const makeScene = (over: Partial<CommandPaneScene> = {}): CommandPaneScene => ({
    displayStacks: STACKS, displaySelectedId: 's1', visibleTopIndices: {}, fanRange: null,
    selectDisplayId: vi.fn(), setFannedOutStackId: vi.fn(), actions: makeActions(),
    mainStacks: STACKS, selectedStackId: 's1', isZoomedOut: false, toggleZoom: vi.fn(),
    capabilities: { shown: true, toggle: vi.fn(), create: vi.fn() },
    ...over,
});

/** The Keyboard group for the default target (cell `a`, the front of
 *  Frame): every registry shortcut no command carries, by context then
 *  label, then the reset row. */
const KEYBOARD_ROWS = [
    'Back / cancel', 'Fullscreen cell', 'Grow cell width', 'Move focused cell left', 'Move focused cell right',
    'New cell + run options', 'Open the Commands pane', 'Reset cell width', 'Select the cell to the left',
    'Select the cell to the right', 'Shrink cell width', 'Toggle the Commands pane',
    'Close the list', 'Highlight the next row', 'Highlight the previous row', 'Run the highlighted row',
    'Align focused stack left', 'Align focused stack right', 'Center focused stack', 'Duplicate cell + run options',
    'Duplicate cell to new stack', 'Duplicate stack', 'Edit cell title', 'Remove focused cell', 'Remove focused stack',
    'Show previous cell in stack',
    'Close file editor', 'Save file',
    'Reset every shortcut to its default',
];

const store = () => useCollimateStore.getState();
const input = () => screen.getByRole('combobox', { name: 'Command' });
/** A row's label: its first span (the meta and the kbd hint follow it). */
const labelOf = (row: HTMLElement) => row.querySelector('span')!.textContent;
const rowLabels = () => screen.queryAllByRole('option').map(labelOf);
const groups = () => [...document.querySelectorAll('[role="listbox"] h4')].map(h => h.textContent);
const option = (name: string) => screen.getAllByRole('option').find(o => labelOf(o) === name)!;
/** A pointer moving over a row: the pane highlights only on a pointer that
 *  moved, so two mousemoves at different spots. */
const hover = (row: HTMLElement) => {
    fireEvent.mouseMove(row, { clientX: 1, clientY: 1 });
    fireEvent.mouseMove(row, { clientX: 2, clientY: 2 });
};

beforeEach(() => {
    useCollimateStore.setState({
        commandPane: { ...defaultCommandPane(), open: true },
        cellLayouts: { a: layout() }, runsMenuOpen: false,
        runs: {}, stoppingRunIds: new Set(), runPhases: {}, lastFinishedByCell: {},
        activeRunsEverywhere: [], pendingGeneratorRunnerStackId: null, maximizedOverlayCellId: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: STACKS, nav_stacks: [] },
        runsPaneView: {}, runsPaneFocus: {}, pendingRunsPaneOpen: null,
    } as never);
});
afterEach(cleanup);

describe('CommandPane', () => {
    it('mounts only while open, as a non-modal pane that is no keyboard owner, on its commands view (K01, C20, CP-3)', () => {
        store().closeCommandPane();
        const { rerender } = render(<CommandPane scene={makeScene()} />);
        expect(screen.queryByRole('complementary')).toBeNull();
        act(() => store().openCommandPane());
        rerender(<CommandPane scene={makeScene()} />);
        const pane = screen.getByRole('complementary', { name: 'Commands' });
        expect(pane).not.toHaveAttribute('aria-modal');
        expect(pane).not.toHaveAttribute('data-owns-keyboard');
        expect(pane).toHaveStyle({ width: '352px' });
        expect(pane.className).toMatch(/\bleft-0\b/);
        expect(pane.className).toMatch(/\bborder-r\b/);
        expect(pane).toHaveAttribute('data-pane-view', 'commands');
        expect(document.activeElement).toBe(input());
    });

    it('names the target cell and lists its commands in the mock\'s groups (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        const target = screen.getByTitle('Commands act on this cell');
        expect(within(target).getByText('Offline editing')).toBeInTheDocument();
        expect(within(target).getByText('Frame')).toBeInTheDocument();
        expect(screen.getByTestId('command-target-light')).toHaveAttribute('data-light', 'idle');
        expect(groups()).toEqual(['This cell', 'Stack', 'Channel', 'Settings', 'Go to', 'Keyboard']);
        expect(rowLabels()).toEqual([
            'Run…', 'Show the log', 'Runs in this cell…', 'Write an annotation…', 'Find file…', 'Import / export…', 'Open tab · b.options_stack', 'Hide files',
            'Dock files on the left', 'Dock files on the top', 'Dock status on the right', 'Dock status on the bottom',
            'Next cell in stack', 'Fan out', 'Split',
            'Hide Capabilities', 'Create a capability…', 'Running…', 'Zoom out to the overview', 'Dock this pane right', 'New stack', 'Settings…',
            'Key manager', 'Editing', 'Themes',
            // Capabilities fronts its manage card: its Go to row is the stack's.
            'CRDT (Yjs)', 'Capabilities',
            ...KEYBOARD_ROWS,
        ]);
        expect(input()).toHaveAttribute('aria-activedescendant', screen.getAllByRole('option')[0].id);
        expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true');
    });

    it('the Dock files rows, left and top, sit under This cell, each with its side\'s panel icon; the files row\'s icon follows the side (FP-03)', () => {
        render(<CommandPane scene={makeScene()} />);
        for (const side of ['left', 'top']) {
            expect(option(`Dock files on the ${side}`).querySelector(`.lucide-panel-${side}`)).not.toBeNull();
        }
        expect(screen.queryByRole('option', { name: /Dock files on the (right|bottom)/ })).toBeNull();
        expect(option('Hide files').querySelector('.lucide-panel-left')).not.toBeNull();
        fireEvent.click(option('Dock files on the top'));
        expect(store().cellLayouts.a.filesPane.side).toBe('top');
        expect(option('Hide files').querySelector('.lucide-panel-top')).not.toBeNull();
    });

    it('the Dock status rows, right and bottom, follow the Dock files rows, each with its side\'s panel icon (CP-03)', () => {
        render(<CommandPane scene={makeScene()} />);
        for (const side of ['right', 'bottom']) {
            expect(option(`Dock status on the ${side}`).querySelector(`.lucide-panel-${side}`)).not.toBeNull();
        }
        const labels = rowLabels();
        expect(labels.indexOf('Dock status on the right')).toBe(labels.indexOf('Dock files on the top') + 1);
        fireEvent.click(option('Dock status on the bottom'));
        expect(store().cellLayouts.a.runsPane).toMatchObject({ side: 'bottom', open: true });
    });

    it('a Capabilities cell has no run or status rows; Find file… and Import / export… stay (CP-03) (D20)', () => {
        useCollimateStore.setState({ cellLayouts: { a: layout(), r: layout() } });
        render(<CommandPane scene={makeScene(onCapabilitiesCell)} />);
        const labels = rowLabels();
        for (const name of ['Run…', 'Show the log', 'Runs in this cell…', 'Write an annotation…', 'Dock status on the right', 'Dock status on the bottom']) {
            expect(labels).not.toContain(name);
        }
        for (const name of ['Find file…', 'Import / export…', 'Hide files', 'Dock files on the left', 'Dock files on the top']) {
            expect(labels).toContain(name);
        }
        // Capabilities never fans out or splits; its first cell steps back to the manage card.
        expect(labels).not.toContain('Fan out');
        expect(labels).not.toContain('Split');
        expect(labels).toContain('Previous cell in stack');
    });

    it('while the manage card is in front there is no target and no cell rows; Alt-↑/↓ still step the stack (D6) (D32)', () => {
        const props = makeScene({ displaySelectedId: 's3', selectedStackId: 's3', visibleTopIndices: { s3: 0 } });
        render(<CommandPane scene={props} />);
        const target = screen.getByTitle('Commands act on this cell');
        expect(target.textContent).toBe('');
        expect(groups()).toEqual(['Channel', 'Settings', 'Go to', 'Keyboard']);
        for (const name of ['Run…', 'Find file…', 'Import / export…', 'Previous cell in stack', 'Next cell in stack']) {
            expect(rowLabels()).not.toContain(name);
        }
        // The selected column is not listed under Go to; the others are, by their front cells.
        expect(rowLabels()).toEqual(expect.arrayContaining(['Offline editing', 'CRDT (Yjs)']));
        expect(rowLabels()).not.toContain('Capabilities');
        fireEvent.keyDown(input(), { key: 'ArrowDown', altKey: true });
        expect(props.actions.winRotate).toHaveBeenCalledWith('s3', 1);
    });

    it('the Go to row for Capabilities is named for the stack while its manage card is in front, else its front cell (CP-03)', () => {
        const props = makeScene();
        const { unmount } = render(<CommandPane scene={props} />);
        expect(option('Capabilities')).toHaveAttribute('data-command', 'goto:capabilities');
        fireEvent.click(option('Capabilities'));
        expect(props.selectDisplayId).toHaveBeenCalledWith('s3');
        unmount();
        render(<CommandPane scene={makeScene({ visibleTopIndices: { s3: 1 } })} />);
        expect(rowLabels()).not.toContain('Capabilities');
        expect(option('Kit')).toHaveAttribute('data-command', 'goto:r');
    });

    it('the Capabilities channel rows read the stack\'s state and run its toggle and Create (CP-03) (D34)', () => {
        const shown = makeScene();
        const { unmount } = render(<CommandPane scene={shown} />);
        expect(rowLabels()).not.toContain('Show Capabilities');
        expect(option('Hide Capabilities')).toHaveAttribute('data-command', 'capabilities');
        fireEvent.click(option('Hide Capabilities'));
        expect(shown.capabilities.toggle).toHaveBeenCalledOnce();
        unmount();
        const hidden = makeScene({
            displayStacks: [frame, options], mainStacks: [frame, options],
            capabilities: { shown: false, toggle: vi.fn(), create: vi.fn() },
        });
        render(<CommandPane scene={hidden} />);
        expect(rowLabels()).not.toContain('Hide Capabilities');
        fireEvent.click(option('Show Capabilities'));
        expect(hidden.capabilities.toggle).toHaveBeenCalledOnce();
        expect(option('Create a capability…')).toHaveAttribute('data-command', 'capabilities-create');
        fireEvent.click(option('Create a capability…'));
        expect(hidden.capabilities.create).toHaveBeenCalledOnce();
        // Create opens a surface outside the pane: the pane closes first.
        expect(store().commandPane.open).toBe(false);
    });

    it('Run… carries the solid Play glyph, the mock\'s i-play (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        expect(option('Run…').querySelector('svg[data-play-glyph] path')).toHaveAttribute('fill', 'currentColor');
    });

    it('every row carries its command key as data-command (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        expect(option('Run…')).toHaveAttribute('data-command', 'run');
        expect(option('Find file…')).toHaveAttribute('data-command', 'findfile');
        expect(screen.getAllByRole('option').every(o => o.getAttribute('data-command'))).toBe(true);
    });

    it('a Capabilities cell shows its own glyph and no cell-run commands; the overview drops the editor commands (K01)', () => {
        const { unmount } = render(<CommandPane scene={makeScene(onCapabilitiesCell)} />);
        expect(screen.getByTestId('command-target-glyph')).toBeInTheDocument();
        expect(screen.queryByTestId('command-target-light')).toBeNull();
        expect(document.querySelector('[data-testid="command-target-glyph"].lucide-monitor')).toBeNull();
        expect(rowLabels()).not.toContain('Run…');
        unmount();
        render(<CommandPane scene={makeScene({ isZoomedOut: true })} />);
        expect(rowLabels()).not.toContain('Run…');
        expect(rowLabels()).not.toContain('Find file…');
        expect(rowLabels()).not.toContain('Hide files');
        expect(rowLabels()).not.toContain('Fan out');
        expect(rowLabels()).toContain('Zoom back in');
    });

    it('filters on label and meta, then on description alone, bolds the match, and says when nothing matches (K01, CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.change(input(), { target: { value: 'fan' } });
        // Fan out by its label; then the shortcuts whose descriptions name a fan.
        expect(rowLabels()).toEqual(['Fan out', 'Back / cancel', 'Select the cell to the left', 'Select the cell to the right']);
        expect(option('Fan out').querySelector('b')!.textContent).toBe('Fan');
        expect(option('Back / cancel').querySelector('b')).toBeNull();
        expect(option('Back / cancel')).toHaveAttribute('title', SHORTCUT_REGISTRY['global.back'].description);
        fireEvent.change(input(), { target: { value: 'title editor' } });
        expect(rowLabels()).toEqual(['Edit cell title']);
        fireEvent.change(input(), { target: { value: 'options' } });
        expect(rowLabels()).toEqual(['Open tab · b.options_stack', 'CRDT (Yjs)', 'New cell + run options', 'Duplicate cell + run options']);
        fireEvent.change(input(), { target: { value: 'zzz' } });
        expect(rowLabels()).toEqual([]);
        expect(screen.getByText('No command matches')).toBeInTheDocument();
    });

    it('↑↓ move with wrap, ↵ runs, a mousemove highlights and a click runs (K01)', () => {
        const props = makeScene();
        render(<CommandPane scene={props} />);
        const rows = () => screen.getAllByRole('option');
        fireEvent.keyDown(input(), { key: 'ArrowUp' });
        expect(input()).toHaveAttribute('aria-activedescendant', rows()[rows().length - 1].id);
        fireEvent.keyDown(input(), { key: 'ArrowDown' });
        expect(input()).toHaveAttribute('aria-activedescendant', rows()[0].id);
        hover(option('Zoom out to the overview'));
        expect(option('Zoom out to the overview')).toHaveAttribute('aria-selected', 'true');
        fireEvent.keyDown(input(), { key: 'Enter' });
        expect(props.toggleZoom).toHaveBeenCalledOnce();
        fireEvent.click(option('CRDT (Yjs)'));
        expect(props.selectDisplayId).toHaveBeenCalledWith('s2');
    });

    it('runs every kind of command against the selected cell (K01)', () => {
        const props = makeScene();
        const channel = store().activeChannel!;
        render(<>
            <RunStatusIndicator activeChannel={channel} isZoomedOut={false} onExitZoom={vi.fn()} onSelectChannel={vi.fn()} />
            <CommandPane scene={props} />
        </>);
        // A command whose surface lies outside the pane closes it first.
        const run = (name: string) => {
            if (!store().commandPane.open) act(() => store().openCommandPane());
            fireEvent.click(option(name));
        };
        run('Run…');
        expect(store().pendingGeneratorRunnerStackId).toBe('s1');
        run('Show the log');
        expect(store().cellLayouts.a.runsPane.open).toBe(true);
        expect(store().runsPaneView.a).toEqual({ view: 'all', option: 'log' });
        // The log takes the keyboard: the pane closes first. There is no
        // Hide row; Show the log only ever shows it.
        expect(store().commandPane.open).toBe(false);
        expect(rowLabels()).not.toContain('Hide the log');
        run('Show the log');
        expect(store().cellLayouts.a.runsPane.open).toBe(true);
        run('Runs in this cell…');
        expect(store().cellLayouts.a.runsPane.open).toBe(true);
        expect(store().runsPaneView.a).toEqual({ view: 'status' });
        expect(store().commandPane.open).toBe(false);
        run('Write an annotation…');
        expect(store().runsPaneView.a).toEqual({ view: 'all', option: 'default' });
        expect(store().runsPaneFocus.a.target).toBe('composer');
        expect(store().commandPane.open).toBe(false);
        run('Dock status on the bottom');
        expect(store().cellLayouts.a.runsPane).toMatchObject({ open: true, side: 'bottom' });
        run('Open tab · b.options_stack');
        expect((store().cellLayouts.a.root as { activeTabId: string }).activeTabId).toBe('t2');
        run('Hide files');
        expect(store().cellLayouts.a.filesPane.collapsed).toBe(true);
        run('Dock files on the top');
        expect(store().cellLayouts.a.filesPane).toMatchObject({ side: 'top', collapsed: false });
        run('Hide files');
        run('Next cell in stack');
        expect(props.actions.winRotate).toHaveBeenCalledWith('s1', 1);
        run('Fan out');
        expect(props.setFannedOutStackId).toHaveBeenCalledWith('s1');
        run('Split');
        expect(props.actions.splitCell).toHaveBeenCalledWith('a', 's1');
        run('Hide Capabilities');
        expect(props.capabilities.toggle).toHaveBeenCalledOnce();
        run('Create a capability…');
        expect(props.capabilities.create).toHaveBeenCalledOnce();
        expect(screen.queryByRole('dialog', { name: 'Running' })).toBeNull();
        run('Running…');
        expect(screen.getByRole('dialog', { name: 'Running' })).toBeInTheDocument();
        expect(store().commandPane.open).toBe(false);
        run('New stack');
        expect(props.actions.newEmptyStackRight).toHaveBeenCalledWith('s1');
        run('Dock this pane right');
        expect(store().commandPane.side).toBe('right');
        expect(screen.getByRole('complementary').className).toMatch(/\bright-0\b/);
        run('Settings…');
        expect(store().commandPane.view).toEqual({ kind: 'settings', section: null });
        expect(screen.getByRole('complementary')).toHaveAttribute('data-pane-view', 'settings');
    });

    it('Find file… opens the Files panel on the selected cell, a Capabilities one too, and leaves the keyboard to it (P03)', () => {
        useCollimateStore.setState({ pickSessions: {}, cellHistory: {}, cellDiffs: {} } as never);
        const { unmount } = render(<CommandPane scene={makeScene()} />);
        // A surface holding the keyboard (a modal keeps it from the pane).
        const modal = document.createElement('div');
        modal.setAttribute('aria-modal', 'true');
        const other = document.createElement('button');
        modal.append(other);
        document.body.append(modal);
        other.focus();
        fireEvent.click(option('Find file…'));
        expect(store().pickSessions.a).toMatchObject({ kind: 'files', query: '' });
        expect(document.activeElement).toBe(other);
        modal.remove();
        unmount();
        render(<CommandPane scene={makeScene(onCapabilitiesCell)} />);
        fireEvent.click(option('Find file…'));
        expect(store().pickSessions.r).toMatchObject({ kind: 'files' });
    });

    it('Import / export… opens the transfer panel on the selected cell, leaving version history first; the panel stays in the cell, so the pane closes (TR-11, CP-4)', () => {
        useCollimateStore.setState({ pickSessions: {}, cellHistory: { a: { nodeId: null, versionId: null, sectionsOpen: { config: true, files: true, logs: true } } }, cellDiffs: {} } as never);
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Import / export…'));
        expect(store().cellHistory.a).toBeUndefined();
        expect(store().pickSessions.a).toMatchObject({ kind: 'transfer', query: '' });
        expect(store().commandPane.open).toBe(false);
    });

    it('a command clears the query and keeps the keyboard in the pane, unless it opened a surface (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.change(input(), { target: { value: 'files' } });
        const modal = document.createElement('div');
        modal.setAttribute('aria-modal', 'true');
        const other = document.createElement('button');
        modal.append(other);
        document.body.append(modal);
        other.focus();
        fireEvent.click(option('Hide files'));
        expect(store().commandPane.query).toBe('');
        expect(document.activeElement).toBe(input());
        other.focus();
        fireEvent.click(option('Run…'));
        expect(document.activeElement).toBe(other);
        modal.remove();
    });

    it('shows the last three commands under Recent while the query is empty (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        for (const name of ['Dock files on the top', 'Fan out', 'New stack', 'Hide files']) fireEvent.click(option(name));
        expect(store().commandPane.recents).toEqual(['files', 'newstack', 'fan']);
        expect(groups()[0]).toBe('Recent');
        expect(rowLabels().slice(0, 3)).toEqual(['Show files', 'New stack', 'Fan out']);
        fireEvent.change(input(), { target: { value: 'n' } });
        expect(groups()).not.toContain('Recent');
    });

    it('Split shows only while the front cell declares .options_stack and runs nothing (K01, C07)', () => {
        useCollimateStore.setState({
            runs: { r1: { runId: 'r1', cellId: 'a', channel: 'ch', runnerId: 'x', runnerName: 'X', generatorName: 'g', label: 'x-1', folder: '_runs/x-1' } },
        } as never);
        const { unmount } = render(<CommandPane scene={makeScene()} />);
        expect(rowLabels()).not.toContain('Split');
        expect(option('Runs in this cell…').textContent).toContain('1 running');
        expect(screen.getByTestId('command-target-light')).toHaveAttribute('data-light', 'running');
        unmount();
        render(<CommandPane scene={makeScene({ displaySelectedId: 's2', selectedStackId: 's2' })} />);
        expect(rowLabels()).not.toContain('Split');
    });

    it('Alt-↑/↓ in the query step the stack and stop at its ends; a fanned stack keeps still (C19)', () => {
        const props = makeScene({ visibleTopIndices: { s1: 1 } });
        const { unmount } = render(<CommandPane scene={props} />);
        expect(rowLabels()).toEqual(expect.arrayContaining(['Previous cell in stack', 'Next cell in stack']));
        fireEvent.keyDown(input(), { key: 'ArrowUp', altKey: true });
        fireEvent.keyDown(input(), { key: 'ArrowDown', altKey: true });
        expect(props.actions.winRotate).toHaveBeenNthCalledWith(1, 's1', -1);
        expect(props.actions.winRotate).toHaveBeenNthCalledWith(2, 's1', 1);
        unmount();
        const atEnd = makeScene({ visibleTopIndices: { s1: 2 } });
        const second = render(<CommandPane scene={atEnd} />);
        expect(rowLabels()).not.toContain('Next cell in stack');
        second.unmount();
        const fanned = makeScene({
            fanRange: { stackId: 's1', name: 'Frame', realIdx: 0, start: 0, end: 2, count: 3 },
        });
        render(<CommandPane scene={fanned} />);
        fireEvent.keyDown(input(), { key: 'ArrowUp', altKey: true });
        expect(fanned.actions.winRotate).not.toHaveBeenCalled();
        expect(rowLabels()).toContain('Collapse');
        expect(rowLabels()).not.toContain('Previous cell in stack');
    });

    it('Esc closes the pane and puts the keyboard on the selected cell (K01)', () => {
        const host = document.createElement('div');
        host.setAttribute('data-cell-id', 'a');
        const card = document.createElement('div');
        card.setAttribute('data-stack-card', '');
        host.append(card);
        document.body.append(host);
        render(<CommandPane scene={makeScene()} />);
        fireEvent.keyDown(input(), { key: 'Escape' });
        expect(store().commandPane.open).toBe(false);
        expect(document.activeElement).toBe(card);
        host.remove();
    });

    it('Esc in the pane goes no further: the selected cell\'s file editor never sees it (polish)', () => {
        // The file editor's Esc listens on the window and closes the file's tab.
        const editorEsc = vi.fn();
        window.addEventListener('keydown', editorEsc);
        render(<CommandPane scene={makeScene()} />);
        fireEvent.keyDown(input(), { key: 'Escape' });
        window.removeEventListener('keydown', editorEsc);
        expect(store().commandPane.open).toBe(false);
        expect(editorEsc).not.toHaveBeenCalled();
    });

    it('the dock switch shows the side pressed and moves the pane (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        const left = screen.getByRole('button', { name: 'Dock left' });
        const right = screen.getByRole('button', { name: 'Dock right' });
        expect(left).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(right);
        expect(right).toHaveAttribute('aria-pressed', 'true');
        expect(left).toHaveAttribute('aria-pressed', 'false');
        const pane = screen.getByRole('complementary');
        expect(pane.className).toMatch(/\bright-0\b/);
        expect(pane.className).toMatch(/\bborder-l\b/);
        expect(document.activeElement).toBe(input());
    });

    it('a press outside closes it in the capture phase and still lands; the Commands button and a modal do not (K01, CP-5)', () => {
        render(<CommandPane scene={makeScene()} />);
        const toggle = document.createElement('button');
        toggle.setAttribute('data-commands-toggle', '');
        const modal = document.createElement('div');
        modal.setAttribute('aria-modal', 'true');
        const inModal = document.createElement('button');
        modal.append(inModal);
        const elsewhere = document.createElement('button');
        const landed = vi.fn(() => expect(store().commandPane.open).toBe(false));
        elsewhere.addEventListener('pointerdown', landed);
        document.body.append(toggle, modal, elsewhere);
        fireEvent.pointerDown(toggle);
        expect(store().commandPane.open).toBe(true);
        fireEvent.pointerDown(input());
        expect(store().commandPane.open).toBe(true);
        fireEvent.pointerDown(inModal);
        expect(store().commandPane.open).toBe(true);
        // A click alone (no pointerdown) leaves the pane open; only a press outside closes it.
        fireEvent.click(elsewhere);
        expect(store().commandPane.open).toBe(true);
        fireEvent.pointerDown(elsewhere);
        expect(landed).toHaveBeenCalledOnce();
        toggle.remove();
        modal.remove();
        elsewhere.remove();
    });

    it('Ctrl-←/→ typed in the query moves the selected cell; in another field they stay word jumps (K01, C19)', () => {
        const selectDisplayId = vi.fn();
        const focusState = {
            focusArea: 'main', selectedWindowId: 's1', mainFocusMode: 'window',
            setFocusArea: vi.fn(), setSelectedWindowId: vi.fn(), setMainFocusMode: vi.fn(),
        } as unknown as FocusContextType;
        renderHook(() => useKeyboardHandler({
            focusState, mainStacks: STACKS, activeChannel: null, performAction: p => p,
            isZoomedOut: false, setIsZoomedOut: vi.fn(), toggleZoom: vi.fn(),
            setTransitionType: vi.fn(), setVisibleTopIndices: vi.fn(), setMergeNonces: vi.fn(),
            visibleTopIndicesRef: { current: {} }, handleRemoveStack: vi.fn(), actions: makeActions(),
            fannedOutStackId: null, setFannedOutStackId: vi.fn(), fan: null,
            displayStacks: STACKS, displaySelectedId: 's1', selectDisplayId,
        }));
        const other = document.createElement('input');
        document.body.append(other);
        fireEvent.keyDown(other, { key: 'ArrowRight', ctrlKey: true });
        expect(selectDisplayId).not.toHaveBeenCalled();
        render(<CommandPane scene={makeScene()} />);
        fireEvent.keyDown(input(), { key: 'ArrowRight', ctrlKey: true });
        expect(selectDisplayId).toHaveBeenCalledWith('s2');
        expect(store().commandPane.open).toBe(true);
        other.remove();
    });

    it('freezes native guests only under the pane (C20)', async () => {
        const guests = await import('@/lib/guestOcclusion');
        const covered = document.createElement('div');
        covered.setAttribute('data-cell-id', 'a');
        covered.getBoundingClientRect = () => new DOMRect(100, 50, 400, 600);
        const clear = document.createElement('div');
        clear.setAttribute('data-cell-id', 'd');
        clear.getBoundingClientRect = () => new DOMRect(900, 50, 400, 600);
        document.body.append(covered, clear);
        const frames: FrameRequestCallback[] = [];
        const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        render(<CommandPane scene={makeScene()} />);
        screen.getByRole('complementary').getBoundingClientRect = () => new DOMRect(0, 0, 320, 800);
        await act(async () => { frames.splice(0).forEach(cb => cb(1000)); });
        expect(guests.isCellOccluded('a')).toBe(true);
        expect(guests.isCellOccluded('d')).toBe(false);
        raf.mockRestore();
        covered.remove();
        clear.remove();
    });
});

describe('CommandPane list order and selection', () => {
    const rows = () => [...document.querySelectorAll<HTMLElement>('[role="option"]')];
    const active = () => input().getAttribute('aria-activedescendant');
    /** Each row's group, read from the heading over it. */
    const rowGroups = () => rows().map(r => r.parentElement!.querySelector('h4')!.textContent);
    /** The groups in row order, one entry per run of rows. */
    const runs = (names: (string | null)[]) => names.filter((g, i) => g !== names[i - 1]);

    // A re-render per row across five lists: about 2 s alone, past the 5 s default under a loaded coverage run.
    it('↓ walks every row in the order shown, across the groups, and wraps (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Fan out'));
        // Typed a key at a time: the list re-renders under each query.
        const field = input();
        for (const query of ['', 'c', 'ce', 'cel', 'cell']) {
            fireEvent.change(field, { target: { value: query } });
            const shown = rows();
            shown.forEach((row, k) => {
                expect(field.getAttribute('aria-activedescendant')).toBe(row.id);
                expect([...document.querySelectorAll('[aria-selected="true"]')]).toEqual([row]);
                if (k < shown.length - 1) fireEvent.keyDown(field, { key: 'ArrowDown' });
            });
            fireEvent.keyDown(field, { key: 'ArrowDown' });
            expect(field.getAttribute('aria-activedescendant')).toBe(shown[0].id);
        }
    }, 20_000);

    it('a query that matches descriptions shows each group once, its rows together, label matches first (K01, CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.change(input(), { target: { value: 'cell' } });
        expect(groups()).toEqual(['This cell', 'Stack', 'Channel', 'Keyboard']);
        expect(runs(rowGroups())).toEqual(groups());
        // Runs in this cell… by its label, then Run… and Find file… by their descriptions.
        expect(rowLabels().slice(0, 3)).toEqual(['Runs in this cell…', 'Run…', 'Find file…']);
        expect(rowLabels().slice(3, 5)).toEqual(['Next cell in stack', 'Fan out']);
    });

    it('only a pointer that moved highlights a row: a mousemove where it already was does not (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        const zoom = option('Zoom out to the overview');
        hover(zoom);
        expect(zoom).toHaveAttribute('aria-selected', 'true');
        fireEvent.keyDown(input(), { key: 'ArrowDown' });
        expect(option('Dock this pane right')).toHaveAttribute('aria-selected', 'true');
        // The rows moved under a still pointer: the browser's mousemove at the same spot.
        fireEvent.mouseMove(zoom, { clientX: 2, clientY: 2 });
        expect(option('Dock this pane right')).toHaveAttribute('aria-selected', 'true');
        fireEvent.mouseMove(zoom, { clientX: 3, clientY: 2 });
        expect(zoom).toHaveAttribute('aria-selected', 'true');
        // Typing under the still pointer keeps the first result highlighted.
        fireEvent.change(input(), { target: { value: 'o' } });
        fireEvent.mouseMove(rows()[2], { clientX: 3, clientY: 2 });
        expect(active()).toBe(rows()[0].id);
    });

    it('typing, a new query and another target put the highlight back on the first row and the list at its top (K01)', () => {
        const { rerender } = render(<CommandPane scene={makeScene()} />);
        const list = screen.getByRole('listbox');
        for (const query of ['e', 'ex', 'e']) {
            fireEvent.keyDown(input(), { key: 'ArrowDown' });
            fireEvent.keyDown(input(), { key: 'ArrowDown' });
            expect(active()).toBe(rows()[2].id);
            list.scrollTop = 120;
            fireEvent.change(input(), { target: { value: query } });
            expect(active()).toBe(rows()[0].id);
            expect(rows()[0]).toHaveAttribute('aria-selected', 'true');
            expect(list.scrollTop).toBe(0);
        }
        fireEvent.change(input(), { target: { value: '' } });
        fireEvent.keyDown(input(), { key: 'ArrowDown' });
        expect(active()).toBe(rows()[1].id);
        rerender(<CommandPane scene={makeScene({ displaySelectedId: 's2', selectedStackId: 's2' })} />);
        expect(active()).toBe(rows()[0].id);
    });

    it('a command under Recent shows once: every row keeps its own id and command (K01)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Hide files'));
        fireEvent.click(option('Fan out'));
        expect(groups()[0]).toBe('Recent');
        expect(groups().filter(g => g === 'Recent')).toHaveLength(1);
        const commands = rows().map(r => r.getAttribute('data-command'));
        const ids = rows().map(r => r.id);
        expect(new Set(commands).size).toBe(commands.length);
        expect(new Set(ids).size).toBe(ids.length);
        expect(commands.slice(0, 2)).toEqual(['fan', 'files']);
        fireEvent.keyDown(input(), { key: 'ArrowDown' });
        expect(active()).toBe(rows()[1].id);
        expect(rows()[1]).toHaveAttribute('data-command', 'files');
    });

    it('two tabs on files of one name are two rows; a file open twice is one (K01)', () => {
        useCollimateStore.setState({
            cellLayouts: {
                a: {
                    root: {
                        type: 'leaf', id: 'g', activeTabId: 't1',
                        tabs: [
                            { id: 't1', kind: 'file', path: 'notes/a.md' },
                            { id: 't2', kind: 'file', path: 'x/readme.md' },
                            { id: 't3', kind: 'file', path: 'y/readme.md' },
                            { id: 't4', kind: 'file', path: 'x/readme.md' },
                        ],
                    },
                    activeLeafId: 'g',
                    filesPane: { ...defaultFilesPane(), collapsed: false },
                    runsPane: defaultRunsPane(),
                } as CellLayout,
            },
        });
        render(<CommandPane scene={makeScene()} />);
        const tabs = rows().filter(r => r.getAttribute('data-command')!.startsWith('tab:'));
        expect(tabs.map(r => r.getAttribute('data-command'))).toEqual(['tab:x/readme.md', 'tab:y/readme.md']);
        expect(tabs.map(labelOf)).toEqual(['Open tab · readme.md', 'Open tab · readme.md']);
    });
});

describe('CommandPane over the full-window view', () => {
    // CellEditor's maximised content region, as the pane meets it: the
    // view's root and its content region.
    const mountView = () => {
        const view = document.createElement('div');
        view.setAttribute('data-full-window', '');
        const region = document.createElement('div');
        region.setAttribute('data-content-region', '');
        view.append(region);
        document.body.append(view);
        return { view, region };
    };
    let view: ReturnType<typeof mountView>;
    beforeEach(() => {
        view = mountView();
        useCollimateStore.setState({ maximizedOverlayCellId: 'a' });
    });
    afterEach(() => { view.view.remove(); });

    it('lies above the view, the window\'s height on its side, with the same hairline and shadow (polish)', () => {
        render(<div data-testid="canvas"><CommandPane scene={makeScene()} /></div>);
        const pane = screen.getByRole('complementary', { name: 'Commands' });
        expect(pane.parentElement).toBe(document.body);
        expect(screen.getByTestId('canvas')).toBeEmptyDOMElement();
        expect(pane.className).toMatch(/\bfixed\b/);
        expect(pane.className).toMatch(/\bz-\[455\]/);
        expect(pane.className).not.toMatch(/\bz-20\b/);
        expect(pane.className).toMatch(/\btop-0\b.*\bbottom-0\b/);
        expect(pane.className).toMatch(/\bleft-0\b/);
        expect(pane.className).toMatch(/\bborder-r\b/);
        expect(pane.className).toMatch(/shadow-\[24px_0_40px_-18px_var\(--lift\)\]/);
        expect(pane.style.getPropertyValue('-webkit-app-region') || (pane.style as unknown as Record<string, string>).WebkitAppRegion).toBe('no-drag');
        expect(document.activeElement).toBe(input());
        act(() => store().setCommandPaneSide('right'));
        expect(screen.getByRole('complementary')).toHaveClass('fixed', 'right-0', 'border-l');
    });

    it('Esc puts the keyboard in the view\'s content, not on the card under it (polish)', () => {
        const host = document.createElement('div');
        host.setAttribute('data-cell-id', 'a');
        const card = document.createElement('div');
        card.setAttribute('data-stack-card', '');
        host.append(card);
        document.body.append(host);
        render(<CommandPane scene={makeScene()} />);
        fireEvent.keyDown(input(), { key: 'Escape' });
        expect(store().commandPane.open).toBe(false);
        expect(document.activeElement).toBe(view.view);
        expect(store().maximizedOverlayCellId).toBe('a');
        host.remove();
    });

    it('"esc to cell" does the same (polish)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(screen.getByRole('button', { name: /to cell/ }));
        expect(document.activeElement).toBe(view.view);
    });

    it('commands on the shown cell keep the view; any that reach past it close the view first, then act (polish)', () => {
        const seen: (string | null)[] = [];
        const record = () => { seen.push(store().maximizedOverlayCellId); };
        const actions = makeActions();
        actions.winRotate.mockImplementation(record);
        actions.newEmptyStackRight.mockImplementation(async () => { record(); return null; });
        const props = makeScene({ actions, toggleZoom: vi.fn(record), selectDisplayId: vi.fn(record) });
        render(<CommandPane scene={props} />);
        const run = (name: string) => {
            if (!store().commandPane.open) act(() => store().openCommandPane());
            fireEvent.click(option(name));
        };
        const stays = (name: string) => {
            run(name);
            expect(store().maximizedOverlayCellId, name).toBe('a');
        };
        stays('Show the log');
        stays('Runs in this cell…');
        stays('Dock status on the bottom');
        stays('Open tab · b.options_stack');
        stays('Hide files');
        stays('Dock files on the top');
        stays('Run…');
        stays('Find file…');
        stays('Import / export…');
        stays('Dock this pane right');
        const leaves = (name: string) => {
            act(() => store().setMaximizedOverlayCellId('a'));
            run(name);
            expect(store().maximizedOverlayCellId, name).toBeNull();
        };
        leaves('Next cell in stack');
        leaves('Zoom out to the overview');
        leaves('New stack');
        leaves('CRDT (Yjs)');
        leaves('Running…');
        leaves('Fan out');
        expect(seen).toEqual([null, null, null, null]);
        // The settings live in the pane, over the view.
        act(() => store().setMaximizedOverlayCellId('a'));
        stays('Settings…');
        expect(screen.getByRole('complementary')).toHaveAttribute('data-pane-view', 'settings');
    });

    it('a command on the selected cell closes the view when the view shows another cell (polish)', () => {
        useCollimateStore.setState({ maximizedOverlayCellId: 'd' });
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Show the log'));
        expect(store().maximizedOverlayCellId).toBeNull();
        expect(store().cellLayouts.a.runsPane.open).toBe(true);
        expect(store().runsPaneView.a).toEqual({ view: 'all', option: 'log' });
    });

    it('a target moved off the shown cell (Ctrl-←/→ from the query) closes the view (polish)', () => {
        const { rerender } = render(<CommandPane scene={makeScene()} />);
        expect(store().maximizedOverlayCellId).toBe('a');
        rerender(<CommandPane scene={makeScene({ displaySelectedId: 's2', selectedStackId: 's2' })} />);
        expect(store().maximizedOverlayCellId).toBeNull();
    });

    it('Alt-↑/↓ in the query close the view, then step the stack (polish)', () => {
        const actions = makeActions();
        actions.winRotate.mockImplementation(() => { expect(store().maximizedOverlayCellId).toBeNull(); });
        render(<CommandPane scene={makeScene({ actions })} />);
        fireEvent.keyDown(input(), { key: 'ArrowDown', altKey: true });
        expect(actions.winRotate).toHaveBeenCalledWith('s1', 1);
    });

    it('closing the view brings the pane back into the canvas with the keyboard still in its query (polish)', () => {
        render(<div data-testid="canvas"><CommandPane scene={makeScene()} /></div>);
        expect(document.activeElement).toBe(input());
        fireEvent.click(option('Zoom out to the overview'));
        const pane = screen.getByRole('complementary', { name: 'Commands' });
        expect(pane.parentElement).toBe(screen.getByTestId('canvas'));
        expect(pane.className).toMatch(/\babsolute\b.*\bz-20\b|\bz-20\b.*\babsolute\b/);
        expect(document.activeElement).toBe(input());
    });

    it('freezes the shown cell\'s guests only while its region reaches under the pane, never the card under the view (polish)', async () => {
        const guests = await import('@/lib/guestOcclusion');
        const card = document.createElement('div');
        card.setAttribute('data-cell-id', 'd');
        card.getBoundingClientRect = () => new DOMRect(100, 50, 400, 600);
        document.body.append(card);
        let regionRect = new DOMRect(0, 0, 1400, 900);
        view.region.getBoundingClientRect = () => regionRect;
        const frames: FrameRequestCallback[] = [];
        const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        render(<CommandPane scene={makeScene()} />);
        screen.getByRole('complementary').getBoundingClientRect = () => new DOMRect(0, 0, 320, 900);
        await act(async () => { frames.splice(0).forEach(cb => cb(1000)); });
        expect(guests.isCellOccluded('a')).toBe(true);
        expect(guests.isCellOccluded('d')).toBe(false);
        regionRect = new DOMRect(320, 0, 1080, 900);
        await act(async () => { frames.splice(0).forEach(cb => cb(2000)); });
        expect(guests.isCellOccluded('a')).toBe(false);
        raf.mockRestore();
        card.remove();
    });
});

describe('CommandPane keyboard rows and command bindings', () => {
    afterEach(() => useKeybindingsStore.setState({ overrides: {}, commands: {}, capturing: null }));

    const kbdOf = (name: string) => option(name).querySelector('kbd');

    it('every registry shortcut no command carries is a Keyboard row with its combo and context, run through fireShortcut (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        const carried = new Set(['stack.run', 'global.findFile', 'stack.rotateNext', 'scene.fanOut', 'global.toggleZoom', 'stack.newCell', 'global.openSettings']);
        const keyboardRows = screen.getAllByRole('option').filter(o => o.getAttribute('data-command')!.startsWith('shortcut:'));
        expect(keyboardRows.map(o => o.getAttribute('data-command')!.slice('shortcut:'.length)).sort())
            .toEqual(allShortcuts().map(e => e.id).filter(id => !carried.has(id)).sort());
        for (const row of keyboardRows) {
            const entry = SHORTCUT_REGISTRY[row.getAttribute('data-command')!.slice('shortcut:'.length)];
            expect(labelOf(row)).toBe(entry.label);
            expect(within(row).getByText(SHORTCUT_CONTEXT_LABEL[entry.context])).toBeInTheDocument();
            expect(row.querySelector('kbd')!.textContent).toBe(displayComboString(entry.default));
            expect(row.querySelector('kbd')).toHaveAttribute('data-binding', 'default');
        }
        // The carried shortcuts show on their commands' rows.
        expect(kbdOf('Run…')!.textContent).toBe('Alt+R');
        const fired = vi.fn((e: KeyboardEvent) => ({ key: e.key, alt: e.altKey, ctrl: e.ctrlKey, shift: e.shiftKey }));
        document.addEventListener('keydown', fired);
        try {
            fireEvent.click(option('Fullscreen cell'));
            expect(fired).toHaveLastReturnedWith({ key: 'f', alt: true, ctrl: false, shift: false });
            fireEvent.click(option('Duplicate cell + run options'));
            expect(fired).toHaveLastReturnedWith({ key: 'd', alt: true, ctrl: false, shift: true });
        } finally {
            document.removeEventListener('keydown', fired);
        }
        expect(store().commandPane.recents[0]).toBe('shortcut:stack.duplicateRun');
    });

    it('a rebound shortcut fires and shows its combo with the teal fill (CP-2)', () => {
        useKeybindingsStore.getState().setOverride('scene.fullscreen', 'Alt+g');
        render(<CommandPane scene={makeScene()} />);
        expect(kbdOf('Fullscreen cell')!.textContent).toBe('Alt+G');
        expect(kbdOf('Fullscreen cell')).toHaveAttribute('data-binding', 'override');
        expect(kbdOf('Fullscreen cell')!.className).toMatch(/\bbg-teal-soft\b/);
        const fired = vi.fn((e: KeyboardEvent) => e.key);
        document.addEventListener('keydown', fired);
        fireEvent.click(option('Fullscreen cell'));
        document.removeEventListener('keydown', fired);
        expect(fired).toHaveLastReturnedWith('g');
    });

    it('searching a combo finds its row, canonical or as displayed (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.change(input(), { target: { value: 'alt+r' } });
        expect(rowLabels()).toEqual(['Run…']);
        fireEvent.change(input(), { target: { value: 'Alt+Shift+Backspace' } });
        expect(rowLabels()).toEqual(['Remove focused stack']);
        act(() => useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l'));
        fireEvent.change(input(), { target: { value: 'alt+l' } });
        expect(rowLabels()).toEqual(['Show the log']);
    });

    it('searching the macOS glyphs finds the row there (CP-2)', () => {
        Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true });
        try {
            render(<CommandPane scene={makeScene()} />);
            fireEvent.change(input(), { target: { value: '⌥R' } });
            expect(rowLabels()).toEqual(['Run…']);
            expect(kbdOf('Run…')!.textContent).toBe('⌥R');
        } finally {
            delete (navigator as unknown as Record<string, unknown>).platform;
        }
    });

    it('a command binding shows on its row; a clash reads red on both rows (CP-2)', () => {
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+a');
        render(<CommandPane scene={makeScene()} />);
        expect(kbdOf('Show the log')!.textContent).toBe('Alt+A');
        expect(kbdOf('Show the log')).toHaveAttribute('data-binding', 'command');
        expect(kbdOf('Show the log')!.className).toMatch(/\bborder-red\b/);
        expect(within(option('Show the log')).getByText('also Zoom out to the overview')).toHaveClass('text-red');
        expect(within(option('Zoom out to the overview')).getByText('also Show the log')).toHaveClass('text-red');
        expect(document.querySelectorAll('[data-conflict]')).toHaveLength(2);
    });

    it('no two rows clash on the default bindings (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        expect(document.querySelector('[data-conflict]')).toBeNull();
    });

    it('the reset row confirms, then resets every override and command binding (CP-2)', async () => {
        useKeybindingsStore.getState().setOverride('scene.fullscreen', 'Alt+g');
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Reset every shortcut to its default'));
        expect(store().confirmDialog).toMatchObject({ kind: 'confirm', destructive: true });
        await act(async () => store().resolveConfirm(true));
        expect(useKeybindingsStore.getState()).toMatchObject({ overrides: {}, commands: {} });
    });

    it('a command binding fires with the pane closed and open, on the current selection (CP-2)', () => {
        useCollimateStore.setState({ cellLayouts: { a: layout(), d: layout() } });
        const logShown = (id: string) => store().cellLayouts[id].runsPane.open
            && store().runsPaneView[id]?.view === 'all' && (store().runsPaneView[id] as { option?: string }).option === 'log';
        store().closeCommandPane();
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        const { rerender } = render(<CommandPane scene={makeScene()} />);
        expect(screen.queryByRole('complementary')).toBeNull();
        fireEvent.keyDown(document.body, { key: 'l', altKey: true });
        expect([logShown('a'), logShown('d')]).toEqual([true, false]);
        expect(store().commandPane.recents[0]).toBe('log');
        rerender(<CommandPane scene={makeScene({ displaySelectedId: 's2', selectedStackId: 's2' })} />);
        fireEvent.keyDown(document.body, { key: 'l', altKey: true });
        expect([logShown('a'), logShown('d')]).toEqual([true, true]);
        act(() => store().openCommandPane());
        rerender(<CommandPane scene={makeScene({ displaySelectedId: 's2', selectedStackId: 's2' })} />);
        // From the pane's query: a modified chord fires from a text field
        // (the log only ever shows, so both stay shown).
        fireEvent.keyDown(input(), { key: 'l', altKey: true });
        expect([logShown('a'), logShown('d')]).toEqual([true, true]);
    });

    it('a bare-key command binding never fires from a text field (CP-2)', () => {
        useKeybindingsStore.getState().setCommandBinding('log', 'g');
        render(<CommandPane scene={makeScene()} />);
        fireEvent.keyDown(input(), { key: 'g' });
        expect(store().cellLayouts.a.runsPane.open).toBe(false);
        act(() => store().closeCommandPane());
        fireEvent.keyDown(document.body, { key: 'g' });
        expect(store().cellLayouts.a.runsPane.open).toBe(true);
        expect(store().runsPaneView.a).toEqual({ view: 'all', option: 'log' });
    });
});

describe('CommandPane binding capture (Shift+Enter)', () => {
    afterEach(() => useKeybindingsStore.setState({ overrides: {}, commands: {}, capturing: null }));

    const kbdOf = (name: string) => option(name).querySelector('kbd');
    /** Highlight a row by its label and press Shift+Enter in the query. */
    const capture = (name: string) => {
        hover(option(name));
        fireEvent.keyDown(input(), { key: 'Enter', shiftKey: true });
    };

    it('the footer offers ⇧↵ bind (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        const footer = screen.getByRole('button', { name: /to cell/ }).parentElement!;
        const hints = [...footer.querySelectorAll(':scope > span')].map(h => h.textContent);
        expect(hints).toEqual(['↑↓ move', '↵ run', '⇧↵ bind']);
    });

    it('binds a registry row as an override, and its own default clears the override (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        capture('Fullscreen cell');
        expect(useKeybindingsStore.getState().capturing).toBe('shortcut:scene.fullscreen');
        expect(kbdOf('Fullscreen cell')!.textContent).toBe('press keys…');
        expect(kbdOf('Fullscreen cell')).toHaveClass('border-teal', 'bg-teal', 'text-sheet');
        expect(option('Fullscreen cell')).toHaveAttribute('data-capturing', 'true');
        act(() => { fireEvent.keyDown(input(), { key: 'g', code: 'KeyG', altKey: true }); });
        expect(useKeybindingsStore.getState()).toMatchObject({ capturing: null, overrides: { 'scene.fullscreen': 'Alt+g' } });
        expect(kbdOf('Fullscreen cell')!.textContent).toBe('Alt+G');
        expect(kbdOf('Fullscreen cell')).toHaveAttribute('data-binding', 'override');
        capture('Fullscreen cell');
        act(() => { fireEvent.keyDown(input(), { key: 'f', code: 'KeyF', altKey: true }); });
        expect(useKeybindingsStore.getState().overrides).toEqual({});
        expect(kbdOf('Fullscreen cell')).toHaveAttribute('data-binding', 'default');
    });

    it('binds a command row by its key; the binding takes the press from the scene and the bindings (CP-2)', () => {
        useKeybindingsStore.getState().setCommandBinding('fan', 'Alt+l');
        const scene = makeScene();
        render(<CommandPane scene={scene} />);
        capture('Show the log');
        expect(kbdOf('Show the log')!.textContent).toBe('press keys…');
        act(() => { fireEvent.keyDown(input(), { key: 'l', code: 'KeyL', altKey: true }); });
        expect(useKeybindingsStore.getState().commands).toEqual({ fan: 'Alt+l', log: 'Alt+l' });
        // The fan row's binding did not fire on the recorded press.
        expect(scene.setFannedOutStackId).not.toHaveBeenCalled();
        expect(kbdOf('Show the log')).toHaveAttribute('data-binding', 'command');
        expect(kbdOf('Show the log')!.className).toMatch(/\bbg-teal-soft\b/);
        expect(store().cellLayouts.a.runsPane.open).toBe(false);
    });

    it('Esc cancels, a bare modifier waits, a refused chord waits, and Backspace clears (CP-2)', () => {
        useKeybindingsStore.getState().setCommandBinding('log', 'Alt+l');
        render(<CommandPane scene={makeScene()} />);
        capture('Show the log');
        act(() => { fireEvent.keyDown(input(), { key: 'Escape' }); });
        expect(useKeybindingsStore.getState()).toMatchObject({ capturing: null, commands: { log: 'Alt+l' } });
        // Esc went no further: the pane is still open.
        expect(store().commandPane.open).toBe(true);
        capture('Show the log');
        act(() => { fireEvent.keyDown(input(), { key: 'Alt', altKey: true }); });
        expect(useKeybindingsStore.getState().capturing).toBe('log');
        act(() => { fireEvent.keyDown(input(), { key: 'Backspace' }); });
        expect(useKeybindingsStore.getState()).toMatchObject({ capturing: null, commands: {} });
        // A cell move needs a modifier: a bare arrow keeps the capture.
        capture('Move focused cell left');
        act(() => { fireEvent.keyDown(input(), { key: 'ArrowLeft' }); });
        expect(useKeybindingsStore.getState().capturing).toBe('shortcut:nav.cellMoveLeft');
        act(() => { fireEvent.keyDown(input(), { key: 'Delete' }); });
        expect(useKeybindingsStore.getState().capturing).toBeNull();
    });

    it('a Recent row (the highlight on opening) captures for its command, on its one row (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Hide files'));
        expect(groups()[0]).toBe('Recent');
        fireEvent.keyDown(input(), { key: 'Enter', shiftKey: true });
        expect(useKeybindingsStore.getState().capturing).toBe('files');
        const row = document.querySelector('[data-capturing]')!;
        expect(row).toHaveAttribute('data-command', 'files');
        expect(within(row as HTMLElement).getByText('press keys…')).toBeInTheDocument();
        act(() => { fireEvent.keyDown(input(), { key: 'l', code: 'KeyL', altKey: true }); });
        expect(useKeybindingsStore.getState().commands).toEqual({ files: 'Alt+l' });
    });

    it('a press anywhere and the row leaving the list cancel (CP-2)', () => {
        render(<CommandPane scene={makeScene()} />);
        capture('Fan out');
        expect(useKeybindingsStore.getState().capturing).toBe('fan');
        act(() => { fireEvent.pointerDown(input()); });
        expect(useKeybindingsStore.getState().capturing).toBeNull();
        capture('Fan out');
        fireEvent.change(input(), { target: { value: 'zoom' } });
        expect(useKeybindingsStore.getState().capturing).toBeNull();
    });
});

describe('CommandPane settings view', () => {
    afterEach(() => uninstallDesktopShell());
    const pane = () => screen.getByRole('complementary', { name: 'Commands' });

    it('Settings… opens the root: a Settings head, the sections with their descriptions, the Settings file rows, the first row focused (CP-3)', async () => {
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Settings…'));
        expect(pane()).toHaveAttribute('data-pane-view', 'settings');
        expect(screen.queryByRole('combobox')).toBeNull();
        expect(screen.queryByTitle('Commands act on this cell')).toBeNull();
        expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
        const rows = [...pane().querySelectorAll<HTMLElement>('[data-settings-row]')];
        expect(rows.map(r => r.getAttribute('aria-label') ?? r.textContent)).toEqual(['Key manager', 'Editing', 'Themes', 'Export settings', 'Import settings']);
        expect(screen.getByRole('button', { name: 'Editing' })).toHaveAccessibleDescription("Spellcheck in this channel's fields, in the app and every renderer");
        expect(screen.getByRole('button', { name: 'Editing' })).toHaveClass('h-11');
        expect(screen.getByText('Settings file')).toBeInTheDocument();
        expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Key manager' }));
        expect(screen.getByText('open')).toBeInTheDocument();
    });

    it('↑↓ step between the root rows with wrap, and ↵ opens a section: back button, title, description, its body (CP-3)', async () => {
        act(() => store().openCommandPaneSettings(null));
        render(<CommandPane scene={makeScene()} />);
        const key = screen.getByRole('button', { name: 'Key manager' });
        fireEvent.keyDown(key, { key: 'ArrowUp' });
        expect(document.activeElement).toHaveAccessibleName('Import settings');
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
        expect(document.activeElement).toHaveAccessibleName('Editing');
        fireEvent.click(document.activeElement!);
        expect(store().commandPane.view).toEqual({ kind: 'settings', section: 'editing' });
        expect(screen.getByRole('heading', { name: 'Editing' })).toBeInTheDocument();
        expect(screen.getByText("Spellcheck in this channel's fields, in the app and every renderer")).toBeInTheDocument();
        expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Back to settings' }));
        expect(await screen.findByLabelText('Editing field')).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Key manager' })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Back to settings' }));
        expect(store().commandPane.view).toEqual({ kind: 'settings', section: null });
        expect(document.activeElement).toHaveAccessibleName('Editing');
    });

    it('Esc steps back: a section → the root → the commands → closed, and never reaches the scene (CP-3)', async () => {
        const editorEsc = vi.fn();
        window.addEventListener('keydown', editorEsc);
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(option('Themes'));
        expect(store().commandPane.view).toEqual({ kind: 'settings', section: 'theme' });
        expect(await screen.findByText('theme body')).toBeInTheDocument();
        fireEvent.keyDown(screen.getByText('theme body'), { key: 'Escape' });
        expect(store().commandPane.view).toEqual({ kind: 'settings', section: null });
        expect(document.activeElement).toHaveAccessibleName('Themes');
        fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
        expect(store().commandPane.view).toEqual({ kind: 'commands' });
        expect(document.activeElement).toBe(input());
        fireEvent.keyDown(input(), { key: 'Escape' });
        expect(store().commandPane.open).toBe(false);
        window.removeEventListener('keydown', editorEsc);
        expect(editorEsc).not.toHaveBeenCalled();
    });

    it('the footer\'s esc back steps back too, and closing resets the view (CP-3)', () => {
        act(() => store().openCommandPaneSettings('keys'));
        render(<CommandPane scene={makeScene()} />);
        fireEvent.click(screen.getByRole('button', { name: /back$/ }));
        expect(store().commandPane.view).toEqual({ kind: 'settings', section: null });
        act(() => store().closeCommandPane());
        act(() => store().openCommandPane());
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
    });

    it('the desktop shell adds Servers to the root and the Settings group; a browser lands a Servers request on the root (CP-3)', async () => {
        installDesktopShell();
        const { unmount } = render(<CommandPane scene={makeScene()} />);
        expect(rowLabels()).toEqual(expect.arrayContaining(['Key manager', 'Editing', 'Themes', 'Servers']));
        fireEvent.click(option('Servers'));
        expect(screen.getByRole('heading', { name: 'Servers' })).toBeInTheDocument();
        expect(await screen.findByText('servers body')).toBeInTheDocument();
        unmount();
        uninstallDesktopShell();
        act(() => store().openCommandPaneSettings('servers'));
        render(<CommandPane scene={makeScene()} />);
        expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Servers' })).toBeNull();
    });

    it('a section body loads only when the section opens (CP-3)', async () => {
        const keys = await import('@/components/settings/KeyManager');
        render(<CommandPane scene={makeScene()} />);
        expect(screen.queryByText('keys body')).toBeNull();
        fireEvent.click(option('Key manager'));
        expect(await screen.findByText('keys body')).toBeInTheDocument();
        expect(keys.KeyManager).toBeDefined();
    });

    it('Mod+K and a press outside close it from settings; the next open is on the commands (CP-3)', () => {
        act(() => store().openCommandPaneSettings('theme'));
        render(<CommandPane scene={makeScene()} />);
        act(() => store().toggleCommandPane());
        expect(screen.queryByRole('complementary')).toBeNull();
        act(() => store().toggleCommandPane());
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
    });
});

describe('CommandPane with no channel', () => {
    afterEach(() => useKeybindingsStore.setState({ overrides: {}, commands: {}, capturing: null }));

    it('lies at the fixed layer over the creation view and offers only the dock row and Settings (CP-3)', () => {
        render(<div data-testid="host"><CommandPane scene={null} /></div>);
        const pane = screen.getByRole('complementary', { name: 'Commands' });
        expect(pane.parentElement).toBe(document.body);
        expect(screen.getByTestId('host')).toBeEmptyDOMElement();
        expect(pane).toHaveClass('fixed', 'z-[455]');
        expect(groups()).toEqual(['Channel', 'Settings']);
        expect(rowLabels()).toEqual(['Dock this pane right', 'Key manager', 'Editing', 'Themes']);
        expect(screen.getByTitle('Commands act on this cell')).toBeEmptyDOMElement();
        fireEvent.click(option('Editing'));
        expect(pane).toHaveAttribute('data-pane-view', 'settings');
    });

    it('mounts no command bindings (CP-3)', () => {
        useKeybindingsStore.getState().setCommandBinding('dock', 'Alt+l');
        store().closeCommandPane();
        render(<CommandPane scene={null} />);
        fireEvent.keyDown(document.body, { key: 'l', altKey: true });
        expect(store().commandPane.side).toBe('left');
    });
});

describe('CommandPane hosting the selected cell\'s picker', () => {
    /** CellEditor's side, reduced: the cell's panel goes into the pane's
     *  slot while the pane hosts it, else it stays in the cell. */
    function Panel({ cellId }: { cellId: string }) {
        const slot = useCollimateStore(s => (paneHostsPanel(s, cellId) ? s.commandPanePanelSlot : null));
        const kind = useCollimateStore(s => s.pickSessions[cellId]?.kind);
        if (!kind) return null;
        const panel = (
            <div role="dialog" aria-label={`${kind} panel`} data-cell-panel={kind}
                onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); store().closePickSession(cellId, kind); } }}>
                <input aria-label="Panel search" />
            </div>
        );
        return slot ? createPortal(panel, slot) : <div data-testid={`in-cell-${cellId}`}>{panel}</div>;
    }
    const session = (cellId: string, kind: string) =>
        useCollimateStore.setState({ pickSessions: { [cellId]: { kind, query: '' } } } as never);
    const pane = () => screen.getByRole('complementary', { name: 'Commands' });
    const slot = () => pane().querySelector('[data-pane-panel-slot]') as HTMLElement;

    beforeEach(() => useCollimateStore.setState({ selectedCellId: 'a', pickSessions: {}, cellHistory: {}, cellDiffs: {} } as never));
    afterEach(() => useCollimateStore.setState({ selectedCellId: null, pickSessions: {} } as never));

    it('a files or runners session of the selected cell renders in the pane\'s slot, the query and list hidden, its search focused (CP-4)', () => {
        render(<><Panel cellId="a" /><CommandPane scene={makeScene()} /></>);
        expect(slot()).toHaveClass('hidden');
        act(() => session('a', 'files'));
        expect(pane()).toHaveAttribute('data-pane-view', 'picker');
        expect(within(slot()).getByRole('dialog', { name: 'files panel' })).toBeInTheDocument();
        expect(screen.queryByTestId('in-cell-a')).toBeNull();
        expect(slot()).toHaveClass('flex-1', 'min-h-0', 'flex', 'flex-col');
        expect(screen.queryByRole('combobox')).toBeNull();
        expect(screen.queryByRole('listbox')).toBeNull();
        expect(within(screen.getByTitle('Commands act on this cell')).getByText('Offline editing')).toBeInTheDocument();
        expect(document.activeElement).toBe(screen.getByLabelText('Panel search'));
        act(() => session('a', 'runners'));
        expect(pane()).toHaveAttribute('data-pane-view', 'picker');
    });

    it('a transfer session, and another cell\'s, stay in the cell (CP-4)', () => {
        render(<><Panel cellId="a" /><Panel cellId="d" /><CommandPane scene={makeScene()} /></>);
        act(() => session('a', 'transfer'));
        expect(screen.getByTestId('in-cell-a')).toBeInTheDocument();
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
        act(() => session('d', 'files'));
        expect(screen.getByTestId('in-cell-d')).toBeInTheDocument();
        act(() => { session('a', 'files'); useCollimateStore.setState({ selectedCellId: 'd' }); });
        expect(screen.getByTestId('in-cell-a')).toBeInTheDocument();
    });

    it('closing the pane puts the panel back in the cell and clears the slot (CP-4)', () => {
        session('a', 'files');
        render(<><Panel cellId="a" /><CommandPane scene={makeScene()} /></>);
        expect(screen.queryByTestId('in-cell-a')).toBeNull();
        act(() => store().closeCommandPane());
        expect(store().commandPanePanelSlot).toBeNull();
        expect(screen.getByTestId('in-cell-a')).toBeInTheDocument();
        expect(store().pickSessions.a).toMatchObject({ kind: 'files' });
    });

    it('keys inside the hosted panel are its own: its Esc closes the session and the pane\'s query takes the keyboard; Alt-↑/↓ still step the stack (CP-4)', () => {
        session('a', 'files');
        const scene = makeScene();
        render(<><Panel cellId="a" /><CommandPane scene={scene} /></>);
        const search = screen.getByLabelText('Panel search');
        fireEvent.keyDown(search, { key: 'ArrowDown', altKey: true });
        expect(scene.actions.winRotate).toHaveBeenCalledWith('s1', 1);
        fireEvent.keyDown(search, { key: 'Escape' });
        expect(store().pickSessions.a).toBeUndefined();
        expect(store().commandPane.open).toBe(true);
        expect(pane()).toHaveAttribute('data-pane-view', 'commands');
        expect(document.activeElement).toBe(input());
    });

    it('Esc from the pane outside the slot steps back as usual (CP-4)', () => {
        session('a', 'files');
        render(<><Panel cellId="a" /><CommandPane scene={makeScene()} /></>);
        fireEvent.keyDown(screen.getByRole('button', { name: 'Dock left' }), { key: 'Escape' });
        expect(store().commandPane.open).toBe(false);
    });
});

describe('CommandPane holds the keyboard', () => {
    it('focus moving off the pane comes back to the entry field; a modal keeps it (CP-5)', () => {
        render(<CommandPane scene={makeScene()} />);
        const outside = document.createElement('button');
        const modal = document.createElement('div');
        modal.setAttribute('aria-modal', 'true');
        const inModal = document.createElement('button');
        modal.append(inModal);
        document.body.append(outside, modal);
        outside.focus();
        expect(document.activeElement).toBe(input());
        inModal.focus();
        expect(document.activeElement).toBe(inModal);
        act(() => store().openCommandPaneSettings(null));
        outside.focus();
        expect(document.activeElement).toHaveAccessibleName('Key manager');
        outside.remove();
        modal.remove();
    });

    it('a renderer frame taking the keyboard without a press hands it back (CP-5)', () => {
        render(<CommandPane scene={makeScene()} />);
        const frame = document.createElement('iframe');
        document.body.append(frame);
        const active = vi.spyOn(document, 'activeElement', 'get').mockReturnValue(frame);
        const focus = vi.spyOn(input(), 'focus');
        window.dispatchEvent(new FocusEvent('blur'));
        expect(focus).toHaveBeenCalled();
        expect(store().commandPane.open).toBe(true);
        active.mockRestore();
        frame.remove();
    });

    it('a press into a renderer frame (focus arriving under the pointer) closes it, as a press anywhere off it does (CP-5)', () => {
        render(<CommandPane scene={makeScene()} />);
        const frame = document.createElement('iframe');
        document.body.append(frame);
        fireEvent.pointerOver(frame);
        const active = vi.spyOn(document, 'activeElement', 'get').mockReturnValue(frame);
        act(() => { window.dispatchEvent(new FocusEvent('blur')); });
        expect(store().commandPane.open).toBe(false);
        active.mockRestore();
        frame.remove();
    });

    it('focus dropped to the page (a press on its chrome, a dialog closing) comes back to the entry field (CP-5)', async () => {
        render(<CommandPane scene={makeScene()} />);
        expect(fireEvent.mouseDown(screen.getByTitle('Commands act on this cell'))).toBe(false);
        expect(fireEvent.mouseDown(input())).toBe(true);
        act(() => input().blur());
        expect(document.activeElement).toBe(document.body);
        await act(() => new Promise(requestAnimationFrame));
        expect(document.activeElement).toBe(input());
        act(() => input().blur());
        fireEvent.keyDown(document.body, { key: 'a' });
        expect(document.activeElement).toBe(input());
    });

    it('a surface a shortcut opens outside the pane keeps the keyboard: Alt+T leaves the cell title editing (CP-5)', () => {
        const onSave = vi.fn();
        render(<><EditableLabel value="Offline editing" onSave={onSave} isActive /><CommandPane scene={makeScene()} /></>);
        fireEvent.keyDown(input(), { key: 't', code: 'KeyT', altKey: true });
        const title = screen.getByDisplayValue('Offline editing');
        expect(document.activeElement).toBe(title);
        expect(onSave).not.toHaveBeenCalled();
        expect(store().commandPane.open).toBe(true);
    });

    it('over the channel-creation view, the view is no modal over the pane: a press on it closes the pane, its fields do not keep focus (CP-5)', () => {
        render(<CommandPane scene={null} />);
        const view = document.createElement('div');
        view.setAttribute('aria-modal', 'true');
        view.setAttribute('data-channel-create', '');
        const name = document.createElement('input');
        view.append(name);
        document.body.append(view);
        name.focus();
        expect(document.activeElement).not.toBe(name);
        fireEvent.pointerDown(name);
        expect(store().commandPane.open).toBe(false);
        view.remove();
    });

    it('closed, it lets focus go anywhere (CP-5)', () => {
        render(<CommandPane scene={makeScene()} />);
        act(() => store().closeCommandPane());
        const outside = document.createElement('button');
        document.body.append(outside);
        outside.focus();
        expect(document.activeElement).toBe(outside);
        outside.remove();
    });

    it('Tab and Shift+Tab wrap inside the pane (CP-5)', () => {
        render(<CommandPane scene={makeScene()} />);
        const first = screen.getByRole('button', { name: 'Dock left' });
        const last = screen.getByRole('button', { name: /to cell/ });
        last.focus();
        fireEvent.keyDown(last, { key: 'Tab' });
        expect(document.activeElement).toBe(first);
        fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
        expect(document.activeElement).toBe(last);
    });
});
