import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState, type ComponentProps } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { CellPanel, CellPanelConceal, type CellPanelRow } from './CellPanel';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import { RendererConcealContext, useRendererConcealed } from '@/contexts/RendererConcealContext';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { KEYBOARD_OWNER_ATTR } from '@/lib/keyboardOwner';
import { useCollimateStore } from '@/stores/useCollimateStore';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));

const ROWS: CellPanelRow[] = [
    { id: 'a', name: 'Alpha', description: 'First row', tags: ['one'] },
    { id: 'b', name: 'Beta', description: 'Second row', reason: 'Select an account' },
    { id: 'c', name: 'Gamma', disabled: true, reason: 'Does not claim .md' },
];
const MODES = [
    { id: 'run', label: 'Run' },
    { id: 'edit', label: 'Edit' },
    { id: 'skip', label: 'Skip', disabled: true },
    { id: 'delete', label: 'Delete' },
] as const;
type Mode = typeof MODES[number]['id'];

const spies = {
    onClose: vi.fn(), onRun: vi.fn(), onSecondary: vi.fn(), onAdd: vi.fn(), onBack: vi.fn(), onQuery: vi.fn(),
    onConfirm: vi.fn(), onCancel: vi.fn(),
};

type Props = Partial<ComponentProps<typeof CellPanel<Mode>>>;

/** A controlled panel, as a kind wires it: selection, mode and query live
 *  in the caller. */
function Harness(props: Props) {
    const [selectedId, setSelectedId] = useState<string | null>('a');
    const [mode, setMode] = useState<Mode>('run');
    const [query, setQuery] = useState('');
    return <CellPanel<Mode>
        cellId="cell-1" kind="runners" title="Runners"
        searchActions={[{ id: 'runs', label: 'Runs', icon: Plus, onRun: spies.onAdd }]}
        note="Runs share the cell's files."
        modes={MODES} mode={mode} onMode={setMode}
        query={query} onQuery={q => { setQuery(q); spies.onQuery(q); }} placeholder="Search runners"
        rows={ROWS} selectedId={selectedId} onSelect={setSelectedId}
        empty="Nothing matches"
        primary={{ label: `Run ‘${selectedId}’`, icon: Trash2, intent: 'run', onRun: () => spies.onRun(selectedId) }}
        secondary={{ label: 'in new cell', hint: '⇧↵', onRun: spies.onSecondary }}
        hints={[{ keys: 'esc', label: 'close' }]}
        onClose={spies.onClose}
        {...props}
    />;
}

const panel = () => screen.getByRole('dialog', { name: 'Runners' });
const search = () => screen.getByRole('combobox', { name: 'Search runners' });
const options = () => screen.getAllByRole('option');
const selected = () => options().findIndex(o => o.getAttribute('aria-selected') === 'true');
const mode = () => within(screen.getByRole('group', { name: 'Mode' })).getAllByRole('button').find(b => b.getAttribute('aria-pressed') === 'true')?.textContent;

beforeEach(() => {
    vi.clearAllMocks();
    useCollimateStore.setState({ commandPane: { ...useCollimateStore.getState().commandPane, open: false } });
});
afterEach(cleanup);

describe('CellPanel', () => {
    it('is the mock sheet over the skin scrim: at most 640px wide with a 12px side buffer, a line-2 edge, radius and shadow, z 5 over a z 4 --lift scrim (P01, RS-05)', () => {
        const view = render(<Harness />);
        const scrim = view.container.querySelector('[data-cell-panel-scrim]')!;
        expect(scrim.className).toMatch(/\babsolute inset-0 z-4 bg-\(--lift\)(\s|$)/);
        expect(scrim.className).not.toMatch(/bg-canvas/);
        for (const cls of ['@container', 'absolute', 'inset-x-[max(0.75rem,calc((100%-40rem)/2))]', 'top-6', 'z-5', 'bg-sheet', 'border-line-2', 'rounded-lg', 'shadow-(--shadow)']) {
            expect(panel().className.split(' ')).toContain(cls);
        }
        expect(panel()).toHaveAttribute('data-cell-panel', 'runners');
        expect(panel()).not.toHaveAttribute('aria-modal');
    });

    it('the scrim reaches the cell\'s bottom and the sheet ends 12px above it (polish)', () => {
        const view = render(<Harness />);
        const scrim = view.container.querySelector('[data-cell-panel-scrim]')!;
        expect(scrim.className.split(' ')).toContain('inset-0');
        expect(panel().className.split(' ')).toContain('bottom-6');
        expect(panel().className.split(' ')).not.toContain('inset-2');
        expect(panel().className.split(' ')).not.toContain('inset-x-2');
        expect(panel().className).not.toMatch(/\b(translate|transform|max-w)-/);
    });

    it('has a 36px header: the uppercase title, quiet actions and Close (P01)', () => {
        render(<Harness />);
        const title = screen.getByText('Runners', { selector: 'span' });
        expect(title.className).toMatch(/uppercase/);
        expect(title.className).toMatch(/text-\[0\.71875rem\]/);
        expect(title.parentElement!.className).toMatch(/\bh-9\b/);
        fireEvent.click(screen.getByRole('button', { name: 'Runs' }));
        expect(spies.onAdd).toHaveBeenCalledOnce();
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('close');
    });

    it('shows the note in a sheet-2 box (P01)', () => {
        render(<Harness />);
        const note = screen.getByTestId('cell-panel-note');
        expect(note.textContent).toBe("Runs share the cell's files.");
        expect(note.className).toMatch(/\bbg-sheet-2\b/);
        expect(note.className).toMatch(/text-\[0\.78125rem\]/);
    });

    it('cycles the live modes with Tab and Shift+Tab from the search box, skipping a disabled one, with a "tab to toggle" hint (P01)', () => {
        render(<Harness />);
        expect(screen.getByText('to toggle').textContent).toBe('tab to toggle');
        expect(mode()).toBe('Run');
        fireEvent.keyDown(search(), { key: 'Tab' });
        expect(mode()).toBe('Edit');
        fireEvent.keyDown(search(), { key: 'Tab' });
        expect(mode()).toBe('Delete');
        fireEvent.keyDown(search(), { key: 'Tab' });
        expect(mode()).toBe('Run');
        fireEvent.keyDown(search(), { key: 'Tab', shiftKey: true });
        expect(mode()).toBe('Delete');
        expect(document.activeElement).toBe(search());
        fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
        expect(mode()).toBe('Edit');
        expect(document.activeElement).toBe(search());
    });

    it('shows "tab to toggle" only while Tab has another live mode to go to', () => {
        render(<Harness modes={[MODES[0], { ...MODES[1], disabled: true }]} />);
        expect(screen.queryByText('to toggle')).toBeNull();
        fireEvent.keyDown(search(), { key: 'Tab' });
        expect(mode()).toBe('Run');
    });

    it('rules the active mode on the cell-tab line: brass in the selected cell, teal elsewhere (P01)', () => {
        const view = render(<Harness />);
        const active = () => screen.getByRole('button', { name: 'Run' });
        expect(active().className).toMatch(/after:bg-teal/);
        expect(active().className).not.toMatch(/after:bg-brass/);
        expect(active().className).toMatch(/h-\[1\.125rem\]/);
        view.unmount();
        render(<CellSelectionContext.Provider value><Harness /></CellSelectionContext.Provider>);
        expect(active().className).toMatch(/after:bg-brass/);
        expect(active().className).not.toMatch(/after:bg-teal/);
    });

    it('the search box keeps the keys: typing searches, the arrows move the selection with wrap, Enter runs the primary (P01)', () => {
        render(<Harness />);
        expect(document.activeElement).toBe(search());
        fireEvent.change(search(), { target: { value: 'al' } });
        expect(spies.onQuery).toHaveBeenLastCalledWith('al');
        expect(selected()).toBe(0);
        fireEvent.keyDown(search(), { key: 'ArrowUp' });
        expect(selected()).toBe(2);
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        expect(selected()).toBe(0);
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        expect(selected()).toBe(1);
        expect(search()).toHaveAttribute('aria-activedescendant', options()[1].id);
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(spies.onRun).toHaveBeenCalledOnce();
    });

    it('lists rows with name, description, tags and reason; the selected row is raised; click selects and double-click runs the primary (P01)', () => {
        render(<Harness />);
        expect(screen.getByRole('listbox', { name: 'Runners' })).toBeTruthy();
        expect(options()[0]).toHaveAttribute('aria-selected', 'true');
        expect(options()[0].className).toMatch(/\bbg-sheet-2\b/);
        expect(within(options()[0]).getByText('Alpha').className).toMatch(/\btext-ink\b/);
        expect(within(options()[1]).getByText('Beta').className).toMatch(/\btext-ink-2\b/);
        expect(within(options()[0]).getByText('one')).toBeTruthy();
        expect(screen.getByText('Select an account').className).toMatch(/\btext-red\b/);
        expect(screen.getByText('Does not claim .md').className).toMatch(/\btext-ink-3\b/);
        fireEvent.click(options()[1]);
        expect(selected()).toBe(1);
        expect(spies.onRun).not.toHaveBeenCalled();
        fireEvent.doubleClick(options()[0]);
        expect(selected()).toBe(0);
        expect(spies.onRun).toHaveBeenCalledOnce();
        // A disabled row selects but never runs.
        fireEvent.doubleClick(options()[2]);
        expect(options()[2]).toHaveAttribute('aria-disabled', 'true');
        expect(spies.onRun).toHaveBeenCalledOnce();
    });

    it('a row note sits quietly right after the description; rows without one show none (GN-01)', () => {
        render(<Harness rows={[{ ...ROWS[0], note: 'will use brief.md' }, ROWS[1]]} />);
        const note = within(options()[0]).getByTestId('cell-panel-row-note');
        expect(note.textContent).toBe('will use brief.md');
        expect(note.className).toMatch(/\btext-ink-3\b/);
        expect(note.previousElementSibling?.textContent).toBe('First row');
        expect(within(options()[1]).queryByTestId('cell-panel-row-note')).toBeNull();
    });

    it('a status tag carries its tone as a dot: pass green, live teal, none ink-3; a plain tag has no dot (X01)', () => {
        render(<Harness rows={[{ id: 'a', name: 'Alpha', tags: ['.md', { label: 'Accepted', tone: 'pass' }, { label: 'Building', tone: 'live' }, { label: 'Not built' }] }]} />);
        const dot = (label: string) => within(options()[0]).getByText(label).querySelector('[aria-hidden]');
        expect(dot('Accepted')?.className).toMatch(/\bbg-green\b/);
        expect(dot('Building')?.className).toMatch(/\bbg-teal\b/);
        expect(dot('Not built')?.className).toMatch(/\bbg-ink-3\b/);
        expect(within(options()[0]).getByText('Accepted')).toHaveAttribute('data-tone', 'pass');
        expect(within(options()[0]).getByText('.md').querySelector('[aria-hidden]')).toBeNull();
    });

    it('a bare double-click runs the primary on the row it lands on, never the one selected before (P01)', () => {
        render(<Harness />);
        expect(selected()).toBe(0);
        // No click before it, as jsdom and some assistive tech send it.
        fireEvent.doubleClick(options()[1]);
        expect(selected()).toBe(1);
        expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('b');
    });

    it('the clicked row\'s primary decides whether a bare double-click runs: refused there, no run (P01)', () => {
        /** A kind whose primary refuses Beta (a broken runner). */
        function Refusing() {
            const [selectedId, setSelectedId] = useState<string | null>('a');
            return <CellPanel cellId="cell-1" kind="runners" title="Runners" rows={ROWS} selectedId={selectedId} onSelect={setSelectedId}
                primary={{ label: 'Run', icon: Trash2, intent: 'run', disabled: selectedId === 'b', onRun: () => spies.onRun(selectedId) }}
                onClose={spies.onClose} />;
        }
        render(<Refusing />);
        fireEvent.doubleClick(options()[1]);
        expect(selected()).toBe(1);
        expect(spies.onRun).not.toHaveBeenCalled();
        fireEvent.doubleClick(options()[0]);
        expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('a');
    });

    it('a bare double-click on the selected row runs it once (P01)', () => {
        render(<Harness />);
        fireEvent.doubleClick(options()[0]);
        expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('a');
    });

    it('shows the empty text when no row is left (P01)', () => {
        render(<Harness rows={[]} />);
        expect(screen.getByText('Nothing matches')).toBeTruthy();
    });

    it('an empty action shows a button under the empty text only while the list is empty', () => {
        const onSelect = vi.fn();
        const view = render(<Harness rows={[]} emptyAction={{ label: 'Open the maker', onSelect }} />);
        const button = screen.getByTestId('cell-panel-empty-action');
        expect(button.textContent).toBe('Open the maker');
        expect(screen.getByRole('listbox').contains(button)).toBe(true);
        fireEvent.click(button);
        expect(onSelect).toHaveBeenCalledOnce();
        expect(spies.onRun).not.toHaveBeenCalled();
        view.rerender(<Harness emptyAction={{ label: 'Open the maker', onSelect }} />);
        expect(screen.queryByTestId('cell-panel-empty-action')).toBeNull();
    });

    it('the footer: hints after "↑↓ select", the secondary on Shift+Enter, the primary with its icon slot and ↵ (P01)', () => {
        render(<Harness />);
        const hints = document.querySelector('[data-cell-panel-hints]')!;
        expect(Array.from(hints.children, hint => hint.textContent)).toEqual(['↑↓ select', 'esc close']);
        const primary = screen.getByTestId('cell-panel-primary');
        expect(primary.textContent).toBe('Run ‘a’↵');
        expect(primary.className).toMatch(/h-\[1\.625rem\]/);
        expect(primary.className).toMatch(/shadow-\[inset_0_0_0_0\.5px_var\(--line\)\]/);
        expect(primary.className).toMatch(/\[&_svg\]:text-brass/);
        fireEvent.click(primary);
        expect(spies.onRun).toHaveBeenCalledOnce();
        const secondary = screen.getByTestId('cell-panel-secondary');
        expect(secondary.textContent).toBe('in new cell⇧↵');
        fireEvent.keyDown(search(), { key: 'Enter', shiftKey: true });
        expect(spies.onSecondary).toHaveBeenCalledOnce();
        expect(spies.onRun).toHaveBeenCalledOnce();
    });

    it('an edit primary\'s icon is quiet; a disabled primary runs on neither Enter nor click and shows why (P01)', () => {
        render(<Harness primary={{ label: 'Edit ‘a’', icon: Trash2, intent: 'edit', disabled: true, reason: 'Pick a runner', onRun: spies.onRun }} />);
        const primary = screen.getByTestId('cell-panel-primary');
        expect(primary.className).toMatch(/\[&_svg\]:text-ink-3/);
        expect(primary.className).not.toMatch(/\[&_svg\]:text-brass/);
        expect(primary.className).toMatch(/disabled:opacity-45/);
        expect(primary).toBeDisabled();
        expect(primary).toHaveAttribute('title', 'Pick a runner');
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(spies.onRun).not.toHaveBeenCalled();
    });

    it('a press on the primary leaves the keyboard in the field; hints without room wrap away whole', () => {
        render(<Harness />);
        expect(document.activeElement).toBe(search());
        // fireEvent returns false once the press's default (moving focus) is prevented.
        expect(fireEvent.mouseDown(screen.getByTestId('cell-panel-primary'))).toBe(false);
        expect(document.querySelector('[data-cell-panel-hints]')!.className).toMatch(/\bflex-wrap\b.*\boverflow-hidden\b/);
    });

    it('with no list, a press where it was keeps the keyboard in the field', () => {
        render(<Harness rows={undefined} />);
        const footer = screen.getByTestId('cell-panel-primary').closest('.border-t')!;
        expect(fireEvent.mouseDown(footer.previousElementSibling!)).toBe(false);
        expect(document.activeElement).toBe(search());
    });

    describe('clickRuns', () => {
        it('a click selects the clicked row and runs its primary, as ↵ does; a disabled row only selects', () => {
            render(<Harness clickRuns />);
            fireEvent.click(options()[1]);
            expect(selected()).toBe(1);
            expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('b');
            fireEvent.click(options()[2]);
            expect(selected()).toBe(2);
            expect(spies.onRun).toHaveBeenCalledOnce();
        });

        it('a double-click runs once: its second press and the dblclick run nothing', () => {
            render(<Harness clickRuns />);
            fireEvent.click(options()[1], { detail: 1 });
            fireEvent.click(options()[0], { detail: 2 });
            fireEvent.doubleClick(options()[0]);
            expect(selected()).toBe(1);
            expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('b');
        });
    });

    it('list keys from a button stay the button\'s: Enter on Close does not run the primary (P01)', () => {
        render(<Harness />);
        const close = screen.getByRole('button', { name: 'Close' });
        close.focus();
        fireEvent.keyDown(close, { key: 'Enter' });
        fireEvent.keyDown(close, { key: 'ArrowDown' });
        expect(spies.onRun).not.toHaveBeenCalled();
        expect(selected()).toBe(0);
    });

    it('Esc closes with reason "cancel"; a click on the scrim closes with "scrim"; either hands the keyboard to the cell (P01)', () => {
        const card = document.createElement('div'); card.setAttribute('data-cell-id', 'cell-1');
        const inner = document.createElement('div'); inner.setAttribute('data-stack-card', ''); card.append(inner); document.body.append(card);
        const view = render(<Harness />);
        fireEvent.keyDown(search(), { key: 'Escape' });
        expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('cancel');
        expect(document.activeElement).toBe(inner);
        fireEvent.click(view.container.querySelector('[data-cell-panel-scrim]')!);
        expect(spies.onClose).toHaveBeenLastCalledWith('scrim');
        card.remove();
    });

    it.each(['cell', 'pane'] as const)('the Esc it handles goes no further than the document, so the cell\'s file editor never closes its tab on it (host %s) (CP-4)', host => {
        const editorEsc = vi.fn();
        window.addEventListener('keydown', editorEsc);
        render(<Harness host={host} />);
        fireEvent.keyDown(search(), { key: 'Escape' });
        expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('cancel');
        expect(editorEsc).not.toHaveBeenCalled();
        window.removeEventListener('keydown', editorEsc);
    });

    describe('a close the panel did not make', () => {
        let inner: HTMLElement;
        beforeEach(() => {
            const card = document.createElement('div'); card.setAttribute('data-cell-id', 'cell-1');
            inner = document.createElement('div'); inner.setAttribute('data-stack-card', ''); card.append(inner); document.body.append(card);
        });
        afterEach(() => { inner.parentElement!.remove(); });

        /** A kind that ends its own session: the primary closes it (a run
         *  started), or hands over to another panel. */
        function Owner({ handOver = false }: { handOver?: boolean }) {
            const [open, setOpen] = useState<'first' | 'second' | null>('first');
            if (open === 'second') return <CellPanel cellId="cell-1" kind="runners" title="Second" query="" onQuery={() => {}} placeholder="Search second" onClose={() => setOpen(null)} />;
            return open ? <Harness primary={{ label: 'Go', icon: Trash2, intent: 'run', onRun: () => setOpen(handOver ? 'second' : null) }} /> : null;
        }

        it('a run that ends the session hands the keyboard to the cell, as Esc does (P02)', () => {
            render(<Owner />);
            expect(document.activeElement).toBe(search());
            fireEvent.keyDown(search(), { key: 'Enter' });
            expect(screen.queryByRole('dialog')).toBeNull();
            expect(document.activeElement).toBe(inner);
        });

        it('so does a primary whose button went disabled under the keyboard (P02)', () => {
            render(<Owner />);
            (document.activeElement as HTMLElement | null)?.blur();
            expect(document.activeElement).toBe(document.body);
            fireEvent.click(screen.getByTestId('cell-panel-primary'));
            expect(screen.queryByRole('dialog')).toBeNull();
            expect(document.activeElement).toBe(inner);
        });

        it('a hand-over to another panel leaves the keyboard in the new panel (P02)', () => {
            render(<Owner handOver />);
            fireEvent.keyDown(search(), { key: 'Enter' });
            expect(screen.getByRole('dialog', { name: 'Second' })).toBeTruthy();
            expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search second' }));
        });

        it('never takes the keyboard from somewhere else (P02)', () => {
            const outside = document.createElement('input'); document.body.append(outside);
            const view = render(<Harness />);
            outside.focus();
            view.unmount();
            expect(document.activeElement).toBe(outside);
            outside.remove();
        });
    });

    it('Esc with the confirm strip up cancels it and keeps the panel (P01)', () => {
        const view = render(<Harness />);
        view.rerender(<Harness confirm={{ message: 'Delete ‘a’?', confirmLabel: 'Delete', onConfirm: spies.onConfirm, onCancel: spies.onCancel }} />);
        const strip = screen.getByText('Delete ‘a’?').parentElement!;
        expect(strip.className).toMatch(/\bbg-red-soft\b/);
        const confirm = within(strip).getByRole('button', { name: 'Delete' });
        expect(document.activeElement).toBe(confirm);
        // Enter on the focused confirm button is the button's, not the primary's.
        fireEvent.keyDown(confirm, { key: 'Enter' });
        expect(spies.onRun).not.toHaveBeenCalled();
        fireEvent.click(confirm);
        expect(spies.onConfirm).toHaveBeenCalledOnce();
        fireEvent.keyDown(confirm, { key: 'Escape' });
        expect(spies.onCancel).toHaveBeenCalledOnce();
        expect(spies.onClose).not.toHaveBeenCalled();
        expect(document.activeElement).toBe(search());
    });

    it('a step replaces the modes, search, list and footer with its body and Back; Esc goes back, not out (P01)', () => {
        render(<Harness step={{ body: <form aria-label="Runner form"><input aria-label="Name" /></form>, onBack: spies.onBack }} />);
        expect(screen.getByRole('form', { name: 'Runner form' })).toBeTruthy();
        for (const gone of [() => screen.queryByRole('listbox'), () => screen.queryByRole('combobox'), () => screen.queryByRole('group', { name: 'Mode' }),
            () => screen.queryByTestId('cell-panel-primary'), () => screen.queryByTestId('cell-panel-note'), () => screen.queryByRole('button', { name: 'Runs' })]) {
            expect(gone()).toBeNull();
        }
        expect(document.activeElement).toBe(document.querySelector('[data-cell-panel-step]'));
        const name = screen.getByLabelText('Name');
        name.focus();
        fireEvent.keyDown(name, { key: 'Enter' });
        fireEvent.keyDown(name, { key: 'Tab' });
        expect(spies.onRun).not.toHaveBeenCalled();
        fireEvent.keyDown(name, { key: 'Escape' });
        expect(spies.onBack).toHaveBeenCalledOnce();
        expect(spies.onClose).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button', { name: 'Back' }));
        expect(spies.onBack).toHaveBeenCalledTimes(2);
    });

    it('below 300px wide the action labels and footer hints hide and the primary moves right (P01)', () => {
        render(<Harness />);
        expect(panel().className.split(' ')).toContain('@container');
        expect(screen.getByText('Runs').className).toMatch(/@max-\[18\.75rem\]:hidden/);
        expect(document.querySelector('[data-cell-panel-hints]')!.className).toMatch(/@max-\[18\.75rem\]:hidden/);
        expect(screen.getByText('in new cell').className).toMatch(/@max-\[18\.75rem\]:hidden/);
        expect(screen.getByTestId('cell-panel-primary').parentElement!.className).toMatch(/@max-\[18\.75rem\]:ml-auto/);
    });

    it('owns the keyboard, marks the pick surface, and freezes only its cell\'s native guests (P01)', () => {
        render(<Harness />);
        expect(panel()).toHaveAttribute(KEYBOARD_OWNER_ATTR, '');
        expect(panel()).toHaveAttribute('data-pick-surface', '');
        expect(panel()).toHaveAttribute('data-pick-cell', 'cell-1');
        expect(useGuestOccluder).toHaveBeenCalledWith({ cellIds: ['cell-1'] }, true);
    });

    it('conceals the pooled renderers under an open panel and passes an ancestor\'s conceal through (P01)', () => {
        const Probe = () => <span data-testid="probe">{String(useRendererConcealed())}</span>;
        const view = render(<CellPanelConceal open={false}><Probe /></CellPanelConceal>);
        expect(screen.getByTestId('probe').textContent).toBe('false');
        view.rerender(<CellPanelConceal open><Probe /></CellPanelConceal>);
        expect(screen.getByTestId('probe').textContent).toBe('true');
        view.rerender(<RendererConcealContext.Provider value><CellPanelConceal open={false}><Probe /></CellPanelConceal></RendererConcealContext.Provider>);
        expect(screen.getByTestId('probe').textContent).toBe('true');
    });

    it('keys typed in one cell\'s panel never move another cell\'s (P01)', () => {
        const other = vi.fn();
        render(<>
            <Harness />
            <CellPanel cellId="cell-2" kind="files" title="Files" rows={ROWS} selectedId="a" onSelect={other}
                primary={{ label: 'Open', icon: Plus, intent: 'run', onRun: other }} onClose={other} />
        </>);
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        fireEvent.keyDown(search(), { key: 'Enter' });
        fireEvent.keyDown(search(), { key: 'Escape' });
        expect(other).not.toHaveBeenCalled();
        expect(spies.onClose).toHaveBeenCalledOnce();
    });

    describe('hosted in the Commands pane (host "pane")', () => {
        it('fills the pane\'s slot flat: no scrim, no inset, edge, radius or shadow, no keyboard owner, no guest freeze; still the pick surface (CP-4)', () => {
            const view = render(<Harness host="pane" />);
            expect(view.container.querySelector('[data-cell-panel-scrim]')).toBeNull();
            const classes = panel().className.split(' ');
            for (const cls of ['@container', 'relative', 'flex', 'flex-1', 'min-h-0', 'flex-col', 'bg-sheet', 'text-[0.8125rem]', 'text-ink', 'outline-none']) {
                expect(classes).toContain(cls);
            }
            for (const cls of ['absolute', 'z-5', 'top-6', 'rounded-lg', 'border', 'border-line-2', 'shadow-(--shadow)']) {
                expect(classes).not.toContain(cls);
            }
            expect(panel()).toHaveAttribute('data-cell-panel-host', 'pane');
            expect(panel()).not.toHaveAttribute(KEYBOARD_OWNER_ATTR);
            expect(useGuestOccluder).toHaveBeenCalledWith({ cellIds: ['cell-1'] }, false);
            expect(useGuestOccluder).not.toHaveBeenCalledWith(expect.anything(), true);
            expect(panel()).toHaveAttribute('data-pick-surface', '');
            expect(panel()).toHaveAttribute('data-pick-cell', 'cell-1');
            expect(panel()).toHaveAttribute('data-cell-panel', 'runners');
        });

        it('keeps its head, modes, search, list, footer and keys (CP-4)', () => {
            render(<Harness host="pane" />);
            expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
            expect(screen.getByRole('group', { name: 'Mode' })).toBeInTheDocument();
            expect(document.activeElement).toBe(search());
            fireEvent.keyDown(search(), { key: 'ArrowDown' });
            expect(selected()).toBe(1);
            fireEvent.keyDown(search(), { key: 'Tab' });
            expect(mode()).toBe('Edit');
            fireEvent.keyDown(search(), { key: 'Enter' });
            expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('b');
            expect(screen.getByTestId('cell-panel-primary')).toBeInTheDocument();
        });

        it('Esc closes it and hands the keyboard to the pane\'s entry field (CP-4)', () => {
            const pane = document.createElement('aside');
            pane.setAttribute('data-command-pane', '');
            const query = document.createElement('input');
            query.setAttribute('data-pane-entry', '');
            pane.append(query);
            document.body.append(pane);
            useCollimateStore.setState({ commandPane: { ...useCollimateStore.getState().commandPane, open: true } });
            render(<Harness host="pane" />, { container: pane.appendChild(document.createElement('div')) });
            fireEvent.keyDown(search(), { key: 'Escape' });
            expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('cancel');
            expect(document.activeElement).toBe(query);
            cleanup();
            pane.remove();
        });
    });

    describe('a location, a field note, a ghost completion and the kind\'s own keys', () => {
        it('shows the location line between the mode tabs and the field, and the field note under the field after its error', () => {
            render(<Harness location={<span>docs/api</span>} fieldNote="Default renderer: Text" error="Name the file." />);
            const location = screen.getByTestId('cell-panel-location');
            const note = screen.getByTestId('cell-panel-field-note');
            expect(location.textContent).toBe('docs/api');
            expect(note.textContent).toBe('Default renderer: Text');
            for (const quiet of [location, note]) {
                expect(quiet.className).toMatch(/text-\[0\.71875rem\]/);
                expect(quiet.className).toMatch(/\btext-ink-3\b/);
            }
            const modes = screen.getByRole('group', { name: 'Mode' });
            const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
            expect(follows(modes, location)).toBe(true);
            expect(follows(location, search())).toBe(true);
            expect(follows(screen.getByRole('alert'), note)).toBe(true);
            expect(follows(note, screen.getByRole('listbox'))).toBe(true);
            expect(search()).toHaveAttribute('aria-describedby', note.id);
        });

        it('shows neither line, nor a description, without them', () => {
            render(<Harness />);
            expect(screen.queryByTestId('cell-panel-location')).toBeNull();
            expect(screen.queryByTestId('cell-panel-field-note')).toBeNull();
            expect(search()).not.toHaveAttribute('aria-describedby');
        });

        it('draws the completion as a ghost suffix after the typed text, marks the field inline-autocomplete, and keeps the field\'s focus as it comes and goes', () => {
            const view = render(<Harness query="notes.m" completion="d" />);
            const ghost = () => document.querySelector('[data-filename-completion]');
            expect(ghost()).toHaveAttribute('aria-hidden', 'true');
            expect(ghost()!.className).toMatch(/\bpointer-events-none\b/);
            const [typed, suffix] = ghost()!.firstElementChild!.children;
            expect(typed.textContent).toBe('notes.m');
            expect(typed.className).toMatch(/\binvisible\b/);
            expect(suffix.textContent).toBe('d');
            expect(suffix.className).toMatch(/\btext-ink-3\b/);
            expect(search()).toHaveAttribute('aria-autocomplete', 'inline');
            // The field keeps room for the suffix at its end, so a name that
            // fills it still shows the completion.
            expect(search().style.paddingRight).toBe('calc(1ch + 0.25rem)');
            const field = search();
            expect(document.activeElement).toBe(field);
            view.rerender(<Harness query="notes.m" />);
            expect(ghost()).toBeNull();
            expect(search()).not.toHaveAttribute('aria-autocomplete');
            expect(search().style.paddingRight).toBe('');
            expect(search()).toBe(field);
            expect(document.activeElement).toBe(field);
        });

        it('a key the kind\'s onFieldKey takes is its own: no move, no mode change, no primary, no close, and the document never sees it', () => {
            const taken: string[] = [];
            const documentKey = vi.fn();
            document.addEventListener('keydown', documentKey);
            render(<Harness onFieldKey={e => { taken.push(e.key); return true; }} />);
            for (const key of ['ArrowDown', 'ArrowUp', 'Tab', 'Enter', 'Escape']) fireEvent.keyDown(search(), { key });
            fireEvent.keyDown(search(), { key: 'Enter', shiftKey: true });
            expect(taken).toEqual(['ArrowDown', 'ArrowUp', 'Tab', 'Enter', 'Escape', 'Enter']);
            expect(selected()).toBe(0);
            expect(mode()).toBe('Run');
            expect(spies.onRun).not.toHaveBeenCalled();
            expect(spies.onSecondary).not.toHaveBeenCalled();
            expect(spies.onClose).not.toHaveBeenCalled();
            expect(documentKey).not.toHaveBeenCalled();
            document.removeEventListener('keydown', documentKey);
        });

        it('a key onFieldKey leaves (false) does what it always does', () => {
            const seen = vi.fn(() => false);
            render(<Harness onFieldKey={seen} />);
            fireEvent.keyDown(search(), { key: 'ArrowDown' });
            expect(selected()).toBe(1);
            fireEvent.keyDown(search(), { key: 'Tab' });
            expect(mode()).toBe('Edit');
            fireEvent.keyDown(search(), { key: 'Enter' });
            expect(spies.onRun).toHaveBeenCalledExactlyOnceWith('b');
            fireEvent.keyDown(search(), { key: 'Escape' });
            expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('cancel');
            expect(seen).toHaveBeenCalledTimes(4);
        });

        it('Esc asks onEscape before closing: true keeps the panel open, false closes it', () => {
            let keep = true;
            const onEscape = vi.fn(() => keep);
            render(<Harness onEscape={onEscape} />);
            fireEvent.keyDown(search(), { key: 'Escape' });
            expect(onEscape).toHaveBeenCalledOnce();
            expect(spies.onClose).not.toHaveBeenCalled();
            keep = false;
            fireEvent.keyDown(search(), { key: 'Escape' });
            expect(onEscape).toHaveBeenCalledTimes(2);
            expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('cancel');
        });

        it('onEscape comes after the confirm strip and the step', () => {
            const onEscape = vi.fn(() => true);
            const view = render(<Harness onEscape={onEscape} confirm={{ message: 'Delete ‘a’?', confirmLabel: 'Delete', onConfirm: spies.onConfirm, onCancel: spies.onCancel }} />);
            fireEvent.keyDown(search(), { key: 'Escape' });
            expect(spies.onCancel).toHaveBeenCalledOnce();
            view.rerender(<Harness onEscape={onEscape} step={{ body: <p>Step</p>, onBack: spies.onBack }} />);
            fireEvent.keyDown(document.querySelector('[data-cell-panel-step]')!, { key: 'Escape' });
            expect(spies.onBack).toHaveBeenCalledOnce();
            expect(onEscape).not.toHaveBeenCalled();
        });
    });

    it('a step can ask with the confirm strip under its body: Esc cancels the question before going Back, and Keep hands the keyboard back to the step', () => {
        const step = { body: <p>Options for ‘a’</p>, onBack: spies.onBack };
        const strip = { message: 'Delete ‘a’?', confirmLabel: 'Delete', onConfirm: spies.onConfirm, onCancel: spies.onCancel };
        const view = render(<Harness step={step} />);
        const body = document.querySelector<HTMLElement>('[data-cell-panel-step]')!;
        expect(document.activeElement).toBe(body);
        view.rerender(<Harness step={step} confirm={strip} />);
        const question = screen.getByText('Delete ‘a’?').parentElement!;
        expect(question.className).toMatch(/\bbg-red-soft\b/);
        expect(body.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        const confirm = within(question).getByRole('button', { name: 'Delete' });
        expect(document.activeElement).toBe(confirm);
        fireEvent.click(confirm);
        expect(spies.onConfirm).toHaveBeenCalledOnce();
        fireEvent.keyDown(confirm, { key: 'Escape' });
        expect(spies.onCancel).toHaveBeenCalledOnce();
        expect(spies.onBack).not.toHaveBeenCalled();
        expect(document.activeElement).toBe(body);
        fireEvent.click(within(question).getByRole('button', { name: 'Keep' }));
        expect(spies.onCancel).toHaveBeenCalledTimes(2);
        expect(document.activeElement).toBe(body);
        view.rerender(<Harness step={step} />);
        expect(screen.queryByText('Delete ‘a’?')).toBeNull();
        fireEvent.keyDown(body, { key: 'Escape' });
        expect(spies.onBack).toHaveBeenCalledOnce();
        expect(spies.onClose).not.toHaveBeenCalled();
    });

    it('a bare panel is the header alone, and still takes the keyboard so Esc closes it (P01)', () => {
        render(<CellPanel cellId="cell-1" kind="transfer" title="Import / export" onClose={spies.onClose} />);
        const bare = screen.getByRole('dialog', { name: 'Import / export' });
        expect(screen.queryByRole('listbox')).toBeNull();
        expect(screen.queryByTestId('cell-panel-primary')).toBeNull();
        expect(document.activeElement).toBe(bare);
        fireEvent.keyDown(bare, { key: 'Escape' });
        expect(spies.onClose).toHaveBeenCalledExactlyOnceWith('cancel');
    });
});
