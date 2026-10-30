import { fileFixture } from '@/test/fileFixtures';
// FilesPane rail (C12, the mock's `.files .rail`): a 30px row of icons —
// the hide toggle, Tree, Folder, History, a spacer, Find file. On-states are
// teal, brass in the selected cell. History stays VISIBLE (greyed, with a
// why-tooltip) on a cell with no history yet (D-O09), and a view button
// leaves history. Hidden, the pane renders nothing (its toggle leads the
// tab bar). The Import / export link sits at the tree's bottom edge.

import { describe, it, expect, vi } from 'vitest';
import { act, render, screen, fireEvent, within } from '@testing-library/react';

import { FilesPane, type FilesPaneProps } from './FilesPane';
import { createDefaultLayout, setFilesPane } from '@/lib/cellLayout';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import type { Cell } from '@/lib/types';
import type { CellLayout } from '@/stores/collimate/types';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

const cell: Cell = { id: 'cell-1', name: 'Test', files: [] };

const baseLayout = () => setFilesPane(createDefaultLayout({
    filePaths: new Set<string>(),
    pinnedPath: null,
}), { collapsed: false });

function renderPane(overrides: Partial<FilesPaneProps> = {}, selected = false) {
    const props: FilesPaneProps = {
        cell,
        layout: baseLayout(),
        onLayoutChange: vi.fn(),
        onOpenFile: vi.fn(),
        onUpdateFiles: vi.fn(),
        readOnly: false,
        activePath: null,
        ...overrides,
    };
    return render(
        <CellSelectionContext.Provider value={selected}>
            <FilesPane {...props} />
        </CellSelectionContext.Provider>,
    );
}

// The minimal history-mode prop: no nodes yet, so the sections render
// their empty state.
const historyProp = (onExit = vi.fn()): NonNullable<FilesPaneProps['history']> => ({
    nodes: [],
    selNodeId: null,
    selVersionId: null,
    nested: false,
    onSelect: vi.fn(),
    onExit,
    sections: {
        node: null,
        loading: false,
        error: null,
        files: [],
        baseFiles: {},
        onToggleNested: vi.fn(),
        sectionsOpen: { config: true, files: true, logs: true },
        onToggleSection: vi.fn(),
        onOpenEntry: vi.fn(),
        activeEntry: null,
    },
});

// The rail: the hide toggle's wrapper's parent (the wrapper carries the
// dock gestures).
const rail = () => screen.getByRole('button', { name: 'Hide files' }).parentElement!.parentElement as HTMLElement;
const dockHandle = () => screen.getByRole('button', { name: 'Hide files' }).parentElement as HTMLElement;
const lastUpdate = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.at(-1)![0] as (l: CellLayout) => CellLayout;

describe('FilesPane rail', () => {
    it('reads hide, Tree, Folder, History, then Find file — icons only, 30px, on the tab strip\'s line (C12)', () => {
        renderPane({ onToggleHistory: vi.fn(), historyDisabled: false });
        const buttons = within(rail()).getAllByRole('button');
        expect(buttons.map(b => b.getAttribute('aria-label'))).toEqual(['Hide files', 'Tree', 'Folder', 'History', 'Find file']);
        expect(buttons.every(b => b.textContent === '' && b.querySelector('svg'))).toBe(true);
        expect(rail().className).toMatch(/\bh-\[1\.875rem\]/);
        // Bottom-aligned 0.25rem above the box's bottom (its 1px line and
        // the rest as padding), as the tab strip's Show files toggle sits
        // (TabStrip TOOL: self-end mb-1 in its 1.875rem strip).
        const railClasses = rail().className.split(/\s+/);
        expect(railClasses).toEqual(expect.arrayContaining(['items-end', 'pb-[calc(0.25rem-1px)]', 'border-b']));
        expect(railClasses).not.toContain('items-start');
        expect(railClasses).not.toContain('pt-px');
        // A spacer pushes Find file to the far end.
        expect(buttons[4].previousElementSibling!.className).toContain('flex-1');
        expect(buttons.every(b => /\bh-\[1.125rem\]/.test(b.className))).toBe(true);
    });

    it('the hide toggle is pressed and hides the pane; hidden, the pane renders nothing (C12)', () => {
        const onLayoutChange = vi.fn();
        const { unmount } = renderPane({ onLayoutChange });
        const hide = screen.getByRole('button', { name: 'Hide files' });
        expect(hide).toHaveAttribute('aria-pressed', 'true');
        expect(hide.querySelector('.lucide-panel-left')).not.toBeNull();
        fireEvent.click(hide);
        expect(lastUpdate(onLayoutChange)(baseLayout()).filesPane.collapsed).toBe(true);
        unmount();

        const { container } = renderPane({ layout: setFilesPane(baseLayout(), { collapsed: true }) });
        expect(container.innerHTML).toBe('');
    });

    it('sets the tree\'s 12.5px ink-2 type for every view under it (C12)', () => {
        renderPane();
        const pane = screen.getByRole('complementary', { name: 'Files' });
        expect(pane.className).toMatch(/text-\[0\.78125rem\]/);
        expect(pane.className).toMatch(/\btext-ink-2\b/);
    });

    it('is a 194px column on the left with one resize strip on its right edge; no dock toggle, no collapse strip (C12, polish)', () => {
        renderPane();
        const pane = screen.getByRole('complementary', { name: 'Files' });
        expect(pane.style.width).toBe('194px');
        expect(pane.className).toMatch(/\bborder-r\b/);
        expect(pane.className).toMatch(/\bflex-none\b/);
        expect(screen.getAllByRole('separator')).toHaveLength(1);
        expect(screen.getByRole('separator')).toHaveAccessibleName('Resize files');
        expect(screen.queryByTitle(/^Dock to/)).toBeNull();
        expect(screen.queryByTitle(/Hide the Files pane/)).toBeNull();
        expect(pane.querySelector('.lucide-chevrons-left, .lucide-chevrons-up')).toBeNull();
    });

    it('the open view is teal outside the selected cell and brass inside it; the rest ink-3 (C12)', () => {
        const { unmount } = renderPane({ onToggleHistory: vi.fn(), historyDisabled: false });
        const tree = screen.getByRole('button', { name: 'Tree' });
        const folder = screen.getByRole('button', { name: 'Folder' });
        expect(tree).toHaveAttribute('aria-pressed', 'true');
        expect(tree.className).toMatch(/\btext-teal\b/);
        expect(tree.className).toMatch(/\bbg-teal-soft\b/);
        expect(folder).toHaveAttribute('aria-pressed', 'false');
        expect(folder.className).toMatch(/\btext-ink-3\b/);
        expect(rail().className).toMatch(/\bborder-line\b/);
        expect(rail().outerHTML).not.toMatch(RETIRED_TOKENS);
        unmount();

        renderPane({ onToggleHistory: vi.fn(), historyDisabled: false }, true);
        const selTree = screen.getByRole('button', { name: 'Tree' });
        expect(selTree.className).toMatch(/\btext-brass\b/);
        expect(selTree.className).toMatch(/\bbg-brass-soft\b/);
        expect(selTree.className).not.toMatch(/\bteal\b/);
    });

    it('Folder switches the view mode (C12)', () => {
        const onLayoutChange = vi.fn();
        renderPane({ onLayoutChange });
        fireEvent.click(screen.getByRole('button', { name: 'Folder' }));
        expect(lastUpdate(onLayoutChange)(baseLayout()).filesPane.viewMode).toBe('folder');
    });

    it('History stays: greyed with a why-tooltip on a cell with no history (C12, D-O09)', () => {
        renderPane({ onToggleHistory: vi.fn(), historyDisabled: true });
        const btn = screen.getByRole('button', { name: 'History' });
        expect(btn).toBeDisabled();
        expect(btn).toHaveAttribute('title', 'No history yet — versions appear after an edit checkpoint or a run');
        expect(btn.querySelector('.lucide-history')).not.toBeNull();
    });

    it('History enters history mode once history exists (C12)', () => {
        const onToggleHistory = vi.fn();
        renderPane({ onToggleHistory, historyDisabled: false });
        const btn = screen.getByRole('button', { name: 'History' });
        expect(btn).toBeEnabled();
        expect(btn).toHaveAttribute('title', 'Version history');
        fireEvent.click(btn);
        expect(onToggleHistory).toHaveBeenCalledTimes(1);
    });

    it('in history mode History is on, hide is refused, and Tree / Folder leave history for that view (C12)', () => {
        const onExit = vi.fn();
        const onToggleHistory = vi.fn();
        const onLayoutChange = vi.fn();
        renderPane({ onToggleHistory, history: historyProp(onExit), onLayoutChange });
        const hist = screen.getByRole('button', { name: 'History' });
        expect(hist).toHaveAttribute('aria-pressed', 'true');
        expect(screen.getByRole('button', { name: 'Tree' })).toHaveAttribute('aria-pressed', 'false');
        expect(screen.getByRole('button', { name: 'Hide files' })).toBeDisabled();
        fireEvent.click(hist);
        expect(onToggleHistory).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', { name: 'Folder' }));
        expect(onExit).toHaveBeenCalledTimes(1);
        expect(lastUpdate(onLayoutChange)(baseLayout()).filesPane.viewMode).toBe('folder');
    });

    it('a right-click on the Hide icon docks on the other side: left, top, left (FP-03)', () => {
        const order = ['left', 'top', 'left'] as const;
        for (let k = 0; k < 2; k++) {
            const onLayoutChange = vi.fn();
            const layout = setFilesPane(baseLayout(), { side: order[k] });
            const { unmount } = renderPane({ onLayoutChange, layout });
            const hide = screen.getByRole('button', { name: 'Hide files' });
            expect(hide).toHaveAttribute('title', `Hide files · right-click: dock on the ${order[k + 1]} · drag to the left or top edge to dock`);
            expect(fireEvent.contextMenu(hide)).toBe(false);
            const next = lastUpdate(onLayoutChange)(layout);
            expect(next.filesPane.side).toBe(order[k + 1]);
            expect(next.filesPane.collapsed).toBe(false);
            unmount();
        }
    });

    it('the Hide icon names the side: PanelLeft, PanelTop (FP-02)', () => {
        for (const side of ['left', 'top'] as const) {
            const { unmount } = renderPane({ layout: setFilesPane(baseLayout(), { side }) });
            const hide = screen.getByRole('button', { name: 'Hide files' });
            expect(hide.querySelector(`.lucide-panel-${side}`)).not.toBeNull();
            expect(hide.querySelectorAll('svg')).toHaveLength(1);
            unmount();
        }
    });

    it('a right-click on the rail or the pane\'s background never moves the pane (FP-03)', () => {
        const onLayoutChange = vi.fn();
        renderPane({ onLayoutChange, cell: { ...cell, files: [fileFixture('a.md', '', { language: 'markdown', timestamp: 't' })] } });
        const aside = screen.getByRole('complementary', { name: 'Files' });
        expect(rail()).not.toHaveAttribute('title');
        fireEvent.contextMenu(rail());
        fireEvent.contextMenu(aside);
        fireEvent.contextMenu(aside.querySelector('[tabindex="-1"]') as HTMLElement);
        expect(onLayoutChange.mock.calls.some(([fn]) => (fn as (l: CellLayout) => CellLayout)(baseLayout()).filesPane.side !== 'left')).toBe(false);
    });

    it('in history mode Hide is disabled, yet its wrapper still cycles the dock side (FP-03)', () => {
        const onLayoutChange = vi.fn();
        renderPane({ onLayoutChange, history: historyProp() });
        const hide = screen.getByRole('button', { name: 'Hide files' });
        expect(hide).toBeDisabled();
        expect(hide.className).toContain('disabled:pointer-events-none');
        expect(dockHandle()).toHaveAttribute('title', 'Leave history to hide files · right-click: dock on the top · drag to the left or top edge to dock');
        expect(fireEvent.contextMenu(dockHandle())).toBe(false);
        expect(lastUpdate(onLayoutChange)(baseLayout()).filesPane.side).toBe('top');
    });

    it('a quick resize-handle click cannot hide active history', () => {
        const onLayoutChange = vi.fn();
        renderPane({ onLayoutChange, history: historyProp() });
        const before = onLayoutChange.mock.calls.length;
        const handle = screen.getByRole('separator', { name: 'Resize files' });
        expect(handle).toHaveAttribute('title', 'Drag to resize the files');
        fireEvent.pointerDown(handle, { clientX: 176, clientY: 100, button: 0, pointerId: 1 });
        fireEvent.pointerUp(handle, { clientX: 176, clientY: 100, pointerId: 1 });
        expect(onLayoutChange).toHaveBeenCalledTimes(before);
    });

    it('a read-only cell has no History button (C12)', () => {
        renderPane({ readOnly: true });
        expect(screen.queryByRole('button', { name: 'History' })).toBeNull();
        expect(within(rail()).getAllByRole('button').map(b => b.getAttribute('aria-label'))).toEqual(['Hide files', 'Tree', 'Folder', 'Find file']);
    });

    it('Find file is greyed on a read-only cell, which shows no panel (P03)', () => {
        renderPane({ readOnly: true });
        expect(screen.getByRole('button', { name: 'Find file' })).toBeDisabled();
        renderPane();
        expect(screen.getAllByRole('button', { name: 'Find file' })[1]).toHaveAttribute('title', expect.stringMatching(/^Find file \(.+P\)$/));
    });

    it('the Import / export link sits at the tree\'s bottom edge as a 24px row, with no footer rule (C12, TR-11)', () => {
        renderPane();
        const link = screen.getByRole('button', { name: 'Import / export' });
        expect(link.querySelector('svg')).not.toBeNull();
        expect(link.className).toMatch(/\bh-6\b/);
        expect(link.className).toMatch(/\btext-\[0\.78125rem\]/);
        const strip = link.parentElement as HTMLElement;
        expect(strip.className).not.toMatch(/\bborder-t\b/);
        expect(strip.parentElement!.lastElementChild).toBe(strip);
    });

    it('Find file is on while the cell\'s Files panel is open in the cell, and off while the Commands pane hosts it (CP-4)', () => {
        useCollimateStore.setState({
            pickSessions: { 'cell-1': { kind: 'files', query: '', at: 1 } }, cellHistory: {}, cellDiffs: {}, selectedCellId: 'cell-1',
            commandPane: { ...defaultCommandPane(), open: true }, commandPanePanelSlot: document.createElement('div'),
        });
        renderPane();
        const find = screen.getByRole('button', { name: 'Find file' });
        expect(find).toHaveAttribute('aria-expanded', 'false');
        act(() => useCollimateStore.setState({ commandPane: defaultCommandPane(), commandPanePanelSlot: null }));
        expect(find).toHaveAttribute('aria-expanded', 'true');
        useCollimateStore.setState({ pickSessions: {}, selectedCellId: null });
    });

    it('Import / export opens the cell\'s transfer panel session (TR-11)', () => {
        useCollimateStore.setState({ pickSessions: {}, cellHistory: {}, activeChannel: null });
        renderPane();
        const link = screen.getByRole('button', { name: 'Import / export' });
        expect(link).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(link);
        expect(useCollimateStore.getState().pickSessions['cell-1']?.kind).toBe('transfer');
        expect(link).toHaveAttribute('aria-expanded', 'true');
        useCollimateStore.setState({ pickSessions: {} });
    });

    it('Import / export is hidden in history mode and greyed on a read-only cell (TR-11)', () => {
        const { unmount } = renderPane({ history: historyProp() });
        expect(screen.queryByRole('button', { name: 'Import / export' })).toBeNull();
        unmount();
        renderPane({ readOnly: true });
        expect(screen.getByRole('button', { name: 'Import / export' })).toBeDisabled();
    });

    it('the files toggle reads pressed while the pane is shown (polish)', () => {
        renderPane();
        const hide = screen.getByRole('button', { name: 'Hide files' });
        expect(hide).toHaveAttribute('aria-pressed', 'true');
        // The top bar's toggle on-state: a --sheet-2 fill, an --ink glyph;
        // a toggle, not a selection (no brass) nor a live state (no teal).
        expect(hide.className).toContain('aria-pressed:bg-sheet-2');
        expect(hide.className).toContain('aria-pressed:text-ink');
        expect(hide.className).not.toMatch(/brass|teal/);
    });
});

// A live run's files are locked per file — never the whole cell. The pane
// shows them with a lock and their owner, carries no run banner, and its
// Import / export link stays live while runs are.
describe('FilesPane — run locks', () => {
    const RUN = {
        runId: 'run-1', cellId: 'cell-1', channel: 'ch', runnerId: 'r', runnerName: 'Runner R',
        generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111',
    };
    const lockOwnerOf = (path: string) => (path.startsWith(`${RUN.folder}/`) ? RUN : null);
    const files = [
        fileFixture('notes.md', '', {language: 'markdown', timestamp: 't'}),
        fileFixture(`${RUN.folder}/chat/conversation.chat`, '', {language: 'plaintext', timestamp: 't'}),
    ];

    it('a locked file wears a lock naming its run; the rest of the cell stays editable', () => {
        renderPane({ cell: { ...cell, files }, lockOwnerOf });
        const locks = screen.getAllByTestId('file-lock');
        // The `_runs` and label folders hold the locked file; the file itself.
        expect(locks.length).toBeGreaterThanOrEqual(1);
        expect(locks[0].getAttribute('title')).toContain('Runner R 11111111');
        expect(screen.queryByText(/Files locked during run/i)).toBeNull();
        expect(screen.queryByText(/Live Files/i)).toBeNull();
    });

    it('the Import / export link stays enabled', () => {
        renderPane({ cell: { ...cell, files }, lockOwnerOf });
        expect(screen.getByRole('button', { name: 'Import / export' })).not.toBeDisabled();
    });
});
