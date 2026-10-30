import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { OverviewCard, OverviewBoardContext, type OverviewBoard } from './OverviewCard';
import { StackGroup, type StackCallbacks } from '../StackGroup';
import { FocusAreaContext } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { OVERVIEW_CARD_HEIGHT, OVERVIEW_COLUMN_WIDTH } from '@/stores/collimate/overviewSlice';
import { createDefaultLayout, openFileTab } from '@/lib/cellLayout';
import type { Cell, RunRecord, Stack, VirtualFile } from '@/lib/types';
import type { JournalEvent } from '@/lib/api/cells';

// The overview card (the mock's cardHtml) with its body at rest, its
// journal style, its Remove and its clicks; and the column it sits in.

vi.mock('../CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('@/lib/api/cells', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/cells')>()),
    fetchChannelTags: vi.fn(async () => [{ tag: 'later', live: 1, removed: 0 }]),
    patchCellMeta: vi.fn(async (_channel: string, _cellId: string, meta: { tags?: string[] }) => ({ tags: meta.tags ?? [] })),
}));
vi.mock('@/lib/api/history', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/history')>()),
    fetchCellHistory: vi.fn(() => new Promise(() => {})),
}));

const initial = useCollimateStore.getState();
beforeEach(() => { useCollimateStore.setState(initial, true); });
afterEach(cleanup);

const file = (name: string, content = '', extra: Partial<VirtualFile> = {}): VirtualFile =>
    (fileFixture(name, content, {language: 'plaintext', timestamp: 't', ...extra}));
const cell = (over: Partial<Cell> = {}): Cell => ({
    id: 'c1', name: 'Before Thursday', files: [file('scratch.md', '# Before Thursday\n\n- Ask')],
    created_at: '2026-10-05T08:20:00', ...over,
});
const run: RunRecord = {
    runId: 'r1', cellId: 'c1', channel: 'ch', runnerId: 'seed-ideas', runnerName: 'Seed ideas',
    generatorName: 'g', label: 'seed-ideas-r1r1r1r1', folder: '_runs/seed-ideas-r1r1r1r1',
};

const mount = (props: Partial<React.ComponentProps<typeof OverviewCard>> = {}, board: OverviewBoard | null = null) => {
    const onOpen = vi.fn();
    const onDragStart = vi.fn();
    const view = render(
        <OverviewBoardContext.Provider value={board}>
            <OverviewCard cell={cell()} selected={false} isOffscreen={false} onOpen={onOpen} onDragStart={onDragStart} {...props} />
        </OverviewBoardContext.Provider>,
    );
    const article = view.container.querySelector('article') as HTMLElement;
    return { ...view, article, onOpen, onDragStart };
};

describe('OverviewCard: the body at rest (O01)', () => {
    it('a native-size sheet with its head: the light and the title (O01)', async () => {
        const { article } = mount();
        expect(article.style.height).toBe('273px');
        expect(article.className.split(/\s+/)).toEqual(expect.arrayContaining(['bg-sheet', 'border-line', 'rounded-lg', 'shadow-(--shadow)']));
        expect(article.dataset.ovCard).toBe('c1');
        const head = article.firstElementChild as HTMLElement;
        expect(head.className).toContain('h-9');
        expect(within(head).getByRole('heading', { name: 'Before Thursday' })).toBeTruthy();
        expect(head.querySelector('.rounded-full')).not.toBeNull(); // the light
    });

    it('the selected card wears the brass edge; a Capabilities cell\'s head is teal with its glyph and no light (O01) (D33)', async () => {
        const { article } = mount({ selected: true });
        expect(article.className).toContain('border-brass');
        cleanup();
        const capabilities = mount({ capabilitiesCell: true, cell: cell({ icon: 'box' }) }).article;
        const head = capabilities.firstElementChild as HTMLElement;
        expect(head.className).toContain('bg-teal-sheet');
        expect(head.querySelector('svg.text-teal')).not.toBeNull();
        expect(head.querySelector('.rounded-full')).toBeNull(); // no light
    });

    it('a Capabilities cell shows no light even while the store holds a run on it (I2) (D33)', async () => {
        useCollimateStore.setState({ runs: { r1: run } } as never);
        const { article } = mount({ capabilitiesCell: true });
        expect((article.firstElementChild as HTMLElement).querySelector('.rounded-full')).toBeNull();
    });

    it('the meta row: the clock, the created time in local time, the author at the right (O01)', async () => {
        mount();
        expect(screen.getByText('5 Oct 2026 · 08:20')).toBeTruthy();
        expect(screen.getByText('you')).toBeTruthy();
    });

    it('at rest the author is the cell\'s maker from the lineage, with no journal loaded (O01)', async () => {
        const end = (cell_id: string) => ({ cell_id, version_id: null });
        useCollimateStore.setState(s => ({ overviewData: { ...s.overviewData, journals: {}, lineage: { cells: [], moves: [
            { id: 1, op: 'duplicate', at: '2026-10-05T08:00:00Z', op_id: 'o1', from: [end('c0')], to: [end('c2')] },
            { id: 2, op: 'run_cell', at: '2026-10-05T08:10:00Z', op_id: 'o2', from: [end('c0')], to: [end('c1')],
                runner: { runner_id: 'seed-ideas', runner_name: 'Seed ideas' } },
        ] } } }));
        mount();
        expect(screen.getByText('Seed ideas')).toBeTruthy();
        cleanup();
        mount({ cell: cell({ id: 'c2' }) });
        expect(screen.getByText('you')).toBeTruthy();
    });

    it('the running runner comes first; the journal style names its newest version\'s author (O01)', async () => {
        const ev = (kind: JournalEvent['kind'], by: JournalEvent['by']): JournalEvent => ({
            t: '2026-10-05T09:00:00Z', kind, by, cells: [{ cell_id: 'c1', name: 'c', status: 'open' }],
            run: null, files: [], files_total: 0, version_id: 'v',
        });
        useCollimateStore.setState(s => ({ overviewData: { ...s.overviewData, journals: { c1: {
            events: [ev('edit', { kind: 'runner', runner_id: 'challenge', runner_name: 'Challenge' })],
            loading: false, complete: true, error: null,
        } } } }));
        // At rest the journal does not name the author.
        mount();
        expect(screen.getByText('you')).toBeTruthy();
        cleanup();
        mount({}, { keep: null, journalCellId: 'c1', followCellId: null });
        expect(screen.getByText('Challenge')).toBeTruthy();
        act(() => { useCollimateStore.setState({ runs: { r1: run } } as never); });
        expect(screen.getByText('Seed ideas · running')).toBeTruthy();
    });

    it('Files (n) names four files, or three and "+n more"; run folders are not the cell\'s files (O01)', async () => {
        const four = ['a.md', 'b.md', 'c.css', 'd.log'].map(n => file(n));
        mount({ cell: cell({ files: [...four, file('_runs/x-1/logs.txt')] }) });
        expect(screen.getByText('Files (4)')).toBeTruthy();
        for (const stem of ['a', 'b', 'c', 'd']) expect(screen.getAllByText(stem).length).toBeGreaterThan(0);
        expect(screen.queryByText(/more/)).toBeNull();
        cleanup();
        mount({ cell: cell({ files: [...four, file('e.md'), file('f.md')] }) });
        expect(screen.getByText('Files (6)')).toBeTruthy();
        expect(screen.getByText('c')).toBeTruthy();
        expect(screen.queryByText('d')).toBeNull();
        expect(screen.getByText('+3 more')).toBeTruthy();
        // The extension in mono.
        expect(screen.getAllByText('.md')[0].className).toContain('font-mono');
    });

    it('a rule, the open file\'s name and its source: the open tab, else the pin, else the first text file (O01)', async () => {
        const files = [file('a.png', '', { body_hash: 'h', encoding: 'binary' }), file('b.md', 'B body'), file('c.md', 'C body'), file('d.md', 'D body')];
        mount({ cell: cell({ files }) });
        expect((await screen.findByText('B body')).tagName).toBe('PRE');
        cleanup();
        mount({ cell: cell({ files, pinned: 'c.md' }) });
        expect(await screen.findByText('C body')).toBeTruthy();
        cleanup();
        useCollimateStore.setState({
            cellLayouts: { c1: openFileTab(createDefaultLayout({ filePaths: files.map(f => f.name), pinnedPath: 'c.md' }), 'd.md', null) },
        } as never);
        const { container } = mount({ cell: cell({ files, pinned: 'c.md' }) });
        expect(await screen.findByText('D body')).toBeTruthy();
        expect(container.querySelector('.h-px.bg-line')).not.toBeNull();
    });

    it('the preview is bounded: the first 14 lines of the first 4096 characters (O01)', async () => {
        const long = Array.from({ length: 5000 }, (_, i) => `line ${i} ${'x'.repeat(40)}`).join('\n');
        const { container } = mount({ cell: cell({ files: [file('big.md', long)] }) });
        await act(async () => {});
        const pre = container.querySelector('pre') as HTMLElement;
        expect(pre.textContent!.split('\n')).toHaveLength(14);
        expect(pre.textContent!.length).toBeLessThanOrEqual(4096);
        cleanup();
        const oneLine = 'y'.repeat(50_000);
        const one = mount({ cell: cell({ files: [file('one.txt', oneLine)] }) });
        await act(async () => {});
        expect(one.container.querySelector('pre')!.textContent).toHaveLength(4096);
    });

    it('an empty text body renders exactly empty, and binary files have no text preview (O01)', async () => {
        mount({ cell: cell({ files: [file('big.md', '', {  })] }) });
        await act(async () => {});
        expect(document.querySelector('pre')?.textContent).toBe('');
        cleanup();
        const { container } = mount({ cell: cell({ files: [file('img.png', '', { body_hash: 'h', encoding: 'binary' })] }) });
        expect(container.querySelector('pre')).toBeNull();
    });
});

describe('OverviewCard: Remove (O01)', () => {
    it('the selected standard card carries Remove in its head, which removes it by id (O01)', async () => {
        const removeFromBoard = vi.fn(async () => true);
        useCollimateStore.setState({ removeFromBoard } as never);
        mount({ selected: true });
        const remove = screen.getByRole('button', { name: 'Remove' });
        expect(remove.getAttribute('title')).toBe('Take it off the board, into Removed');
        expect(remove.className).toContain('border-line-2');
        fireEvent.click(remove);
        expect(removeFromBoard).toHaveBeenCalledExactlyOnceWith('c1');
    });

    it('Remove is disabled, with a title, while the cell has live runs (O01)', async () => {
        useCollimateStore.setState({ runs: { r1: run } } as never);
        const removeFromBoard = vi.fn(async () => true);
        useCollimateStore.setState({ removeFromBoard } as never);
        mount({ selected: true });
        const remove = screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement;
        expect(remove.disabled).toBe(true);
        expect(remove.title).toBe('Stop its runs to remove it');
        fireEvent.click(remove);
        expect(removeFromBoard).not.toHaveBeenCalled();
    });

    it('no Remove on an unselected card or on a Capabilities cell (O01) (D33)', async () => {
        mount();
        expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();
        cleanup();
        mount({ selected: true, capabilitiesCell: true });
        expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();
    });
});

describe('OverviewCard: clicks (O01)', () => {
    it('a click selects through the overview\'s anchor rule; Remove\'s click does not (O01)', async () => {
        const selectCard = vi.fn();
        useCollimateStore.setState({ selectCard, removeFromBoard: vi.fn(async () => true) } as never);
        const { article } = mount({ selected: true });
        fireEvent.click(article);
        expect(selectCard).toHaveBeenCalledExactlyOnceWith('c1');
        fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
        expect(selectCard).toHaveBeenCalledTimes(1);
    });

    it('a press drags the card; a press on Remove does not (O01)', async () => {
        useCollimateStore.setState({ removeFromBoard: vi.fn(async () => true) } as never);
        const { article, onDragStart } = mount({ selected: true });
        fireEvent.pointerDown(screen.getByRole('button', { name: 'Remove' }), { button: 0 });
        expect(onDragStart).not.toHaveBeenCalled();
        fireEvent.pointerDown(article, { button: 0 });
        expect(onDragStart).toHaveBeenCalledTimes(1);
        expect(article.hasAttribute('data-stack-card')).toBe(true);
        cleanup();
        // A card that does not drag does not block the board's pan.
        const still = mount({ onDragStart: undefined });
        expect(still.article.className).toContain('cursor-pointer');
        expect(still.article.hasAttribute('data-stack-card')).toBe(false);
    });
});

describe('OverviewCard: the Tags button (TG-05)', () => {
    it('a card\'s head ends in Tags, before Remove when selected; a Capabilities cell has Tags alone, a built-in card none (TG-05) (D33)', async () => {
        const { article } = mount({ selected: true, cell: cell({ tags: ['draft', 'later'] }) });
        const head = article.firstElementChild as HTMLElement;
        const buttons = within(head).getAllByRole('button');
        expect(buttons.map(b => b.getAttribute('aria-label') ?? b.textContent)).toEqual(['Tags', 'Remove']);
        expect(buttons[0].textContent).toBe('2');
        cleanup();
        mount();
        expect(screen.getByRole('button', { name: 'Tags' })).toBeTruthy();
        cleanup();
        const capabilities = mount({ selected: true, capabilitiesCell: true, cell: cell({ tags: ['kit'] }) }).article;
        const capButtons = within(capabilities.firstElementChild as HTMLElement).getAllByRole('button');
        expect(capButtons.map(b => b.getAttribute('aria-label') ?? b.textContent)).toEqual(['Tags']);
        expect(capButtons[0].textContent).toBe('1');
        cleanup();
        mount({ selected: true, capabilitiesCell: true, cell: cell({ name: 'Definitions', builtin: true }) });
        expect(screen.queryByRole('button', { name: 'Tags' })).toBeNull();
    });

    it('a press on Tags never drags, and nothing in its menu selects the card (TG-05)', async () => {
        const selectCard = vi.fn();
        useCollimateStore.setState({ selectCard } as never);
        const { onDragStart } = mount({ cell: cell({ channel: 'ch', tags: [] }) });
        const trigger = screen.getByRole('button', { name: 'Tags' });
        fireEvent.pointerDown(trigger, { button: 0 });
        fireEvent.click(trigger);
        const dialog = await screen.findByRole('dialog', { name: 'Tags' });
        const later = await screen.findByRole('button', { name: 'later' });
        for (const el of [dialog, within(dialog).getByRole('textbox'), later]) {
            fireEvent.pointerDown(el, { button: 0 });
            fireEvent.click(el);
        }
        expect(onDragStart).not.toHaveBeenCalled();
        expect(selectCard).not.toHaveBeenCalled();
        expect(useCollimateStore.getState().cellCache.c1?.tags).toEqual(['later']);
    });
});

describe('OverviewCard: the tags in the body (the shelf\'s tag toggle)', () => {
    const showTags = (on: boolean) => act(() => { useCollimateStore.getState().setShowTags(on); });

    it('with the toggle off the body is as at rest: no chips (tags toggle)', async () => {
        const { article } = mount({ cell: cell({ tags: ['wip', 'ui'] }) });
        expect(article.querySelector('[data-ov-tags]')).toBeNull();
    });

    it('with the toggle on the body opens with the cell\'s tags as wrapping chips, two lines at most, the card keeping its height (tags toggle)', async () => {
        const { article } = mount({ cell: cell({ tags: ['wip', 'needs review', 'ui'] }) });
        showTags(true);
        const row = article.querySelector<HTMLElement>('[data-ov-tags]')!;
        expect(row).not.toBeNull();
        expect([...row.querySelectorAll('[data-tag-chip]')].map(li => li.textContent)).toEqual(['wip', 'needs review', 'ui']);
        // Everything fits (jsdom measures nothing as overflowing): no "+N" chip.
        expect(row.querySelector<HTMLElement>('[data-tag-more]')!.hidden).toBe(true);
        // In the body, under the head, before the preview.
        const body = article.children[1] as HTMLElement;
        expect(body.firstElementChild).toBe(row);
        expect(within(body).getByText('5 Oct 2026 · 08:20')).toBeTruthy(); // the preview's meta row follows
        expect(row.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex-wrap', 'max-h-[2.5rem]', 'overflow-hidden']));
        expect(article.style.height).toBe('273px');
        // Off again: gone.
        showTags(false);
        expect(article.querySelector('[data-ov-tags]')).toBeNull();
    });

    it('a tagless cell shows nothing with the toggle on (tags toggle)', async () => {
        useCollimateStore.getState().setShowTags(true);
        const { article } = mount({ cell: cell({ tags: [] }) });
        expect(article.querySelector('[data-ov-tags]')).toBeNull();
        cleanup();
        expect(mount({ cell: cell() }).article.querySelector('[data-ov-tags]')).toBeNull();
    });

    it('the journal style opens with the chips too (tags toggle)', async () => {
        useCollimateStore.getState().setShowTags(true);
        const { article } = mount({ cell: cell({ tags: ['wip'] }) }, { keep: null, journalCellId: 'c1', followCellId: null });
        const body = article.children[1] as HTMLElement;
        expect(body.firstElementChild).toHaveAttribute('data-ov-tags');
        expect(body.querySelector('[data-journal-card-body="c1"]')).not.toBeNull();
    });
});

describe('OverviewCard: the journal style and the follow hint (O01)', () => {
    it('the anchor\'s card grows into its journal style, never shorter than a card at rest (O01)', async () => {
        const { article } = mount({}, { keep: null, journalCellId: 'c1', followCellId: null });
        expect(article.style.height).toBe('');
        expect(article.style.minHeight).toBe('273px');
        expect(article.querySelector('[data-journal-card-body="c1"]')).not.toBeNull();
        expect(article.querySelector('pre')).toBeNull();
    });

    it('the journal\'s card wears the teal edge, the brass edge\'s geometry; the selected card without it keeps brass (journal teal)', async () => {
        const classes = (el: HTMLElement) => el.className.split(/\s+/);
        const { article } = mount({}, { keep: null, journalCellId: 'c1', followCellId: null });
        expect(classes(article)).toEqual(expect.arrayContaining(['border', 'rounded-lg', 'border-teal']));
        expect(classes(article)).not.toContain('border-line');
        expect(classes(article)).toContain('transition-[border-color]'); // a colour fade, never opacity
        cleanup();
        const selected = mount({ selected: true }, { keep: null, journalCellId: 'other', followCellId: null }).article;
        expect(classes(selected)).toContain('border-brass');
        expect(classes(selected)).not.toContain('border-teal');
    });

    it('the selected card is teal while its journal is open; brass returns when the journal closes or moves on (journal teal)', async () => {
        const classes = (el: HTMLElement) => el.className.split(/\s+/);
        const at = (journalCellId: string | null) => (
            <OverviewBoardContext.Provider value={{ keep: null, journalCellId, followCellId: null }}>
                <OverviewCard cell={cell()} selected isOffscreen={false} />
            </OverviewBoardContext.Provider>
        );
        const { container, rerender } = render(at('c1'));
        const article = container.querySelector('article') as HTMLElement;
        expect(classes(article)).toContain('border-teal');
        expect(classes(article)).not.toContain('border-brass');
        rerender(at(null)); // the journal closes
        expect(classes(article)).toContain('border-brass');
        expect(classes(article)).not.toContain('border-teal');
        rerender(at('c1'));
        expect(classes(article)).toContain('border-teal');
        rerender(at('c2')); // the journal moves to another cell
        expect(classes(article)).toContain('border-brass');
        expect(classes(article)).not.toContain('border-teal');
    });

    it('the card a second click would follow says so in a 30px foot (O01)', async () => {
        mount({ selected: true }, { keep: null, journalCellId: null, followCellId: 'c1' });
        const foot = screen.getByText('Click again to follow its lines');
        expect(foot.className).toContain('h-[1.875rem]');
    });
});

describe('the overview column (O01)', () => {
    const callbacks: StackCallbacks = {
        onSelect: vi.fn(), onClose: vi.fn(), onRemoveCell: vi.fn(), onUpdate: vi.fn(),
        onStackDragStart: vi.fn(), onCellDragStart: vi.fn(), onZoomToCell: vi.fn(),
    };
    const column = (s: Stack, opts: { selected?: boolean; board?: OverviewBoard | null; preview?: number | null; hidden?: string } = {}) => render(
        <FocusAreaContext.Provider value="main">
            <OverviewBoardContext.Provider value={opts.board ?? { keep: null, journalCellId: null, followCellId: null }}>
                <StackGroup stack={s} width={OVERVIEW_COLUMN_WIDTH} height={OVERVIEW_CARD_HEIGHT} isSelected={!!opts.selected} viewMode="overview"
                    callbacks={callbacks} previewInsertCellIdx={opts.preview ?? null} hiddenCellId={opts.hidden ?? null} />
            </OverviewBoardContext.Provider>
        </FocusAreaContext.Provider>,
    );
    const cells = [
        cell({ id: 'new', name: 'Newest', created_at: '2026-10-05T10:00:00' }),
        cell({ id: 'old', name: 'Oldest', created_at: '2026-10-01T10:00:00' }),
        cell({ id: 'mid', name: 'Middle', created_at: '2026-10-03T10:00:00' }),
    ];

    it('lists its cards in stack order under the shared overview head (polish)', async () => {
        const { container } = column({ id: 's', name: 'Options', cells });
        const order = [...container.querySelectorAll('[data-ov-column] [data-ov-card]')].map(el => el.getAttribute('data-ov-card'));
        expect(order).toEqual(['new', 'old', 'mid']);
        const head = container.querySelector('[data-overview-head]') as HTMLElement;
        expect(head.style.height).toBe('33px');
        expect(within(head).getByText('Options').className).toContain('text-ink-2');
    });

    it('the name is brass on the selected stack and teal on Capabilities; the grip drags the column (O01)', async () => {
        const { container } = column({ id: 's', name: 'Options', cells }, { selected: true });
        expect(screen.getByText('Options').className).toContain('text-brass');
        const grip = container.querySelector('[data-overview-grip]') as HTMLElement;
        // Shown on the column's hover by a colour fade, never opacity.
        expect(grip.className).toContain('group-hover/ovcol:text-ink-3');
        expect(grip.className).not.toMatch(/opacity/);
        fireEvent.pointerDown(grip, { button: 0 });
        expect(callbacks.onStackDragStart).toHaveBeenCalledTimes(1);
        cleanup();
        column({ id: 'cap', name: 'Capabilities', stack_type: 'capabilities', cells }, { selected: true });
        expect(screen.getByText('Capabilities').className).toContain('text-teal');
    });

    it('the Capabilities column head is not renamed: a click on its name opens no field (O01) (D33)', async () => {
        const onRenameStack = vi.fn();
        render(
            <FocusAreaContext.Provider value="main">
                <OverviewBoardContext.Provider value={{ keep: null, journalCellId: null, followCellId: null }}>
                    <StackGroup stack={{ id: 'cap', name: 'Capabilities', stack_type: 'capabilities', cells }} width={OVERVIEW_COLUMN_WIDTH}
                        height={OVERVIEW_CARD_HEIGHT} isSelected viewMode="overview"
                        callbacks={{ ...callbacks, onRenameStack }} previewInsertCellIdx={null} hiddenCellId={null} />
                </OverviewBoardContext.Provider>
            </FocusAreaContext.Provider>,
        );
        fireEvent.click(screen.getByText('Capabilities'));
        expect(screen.queryByRole('textbox')).toBeNull();
        expect(onRenameStack).not.toHaveBeenCalled();
    });

    it('a Capabilities column draws its cells as Capabilities cards: teal heads, no Remove (O01) (D33)', async () => {
        const { container } = column({ id: 'cap', name: 'Capabilities', stack_type: 'capabilities', cells });
        for (const card of container.querySelectorAll<HTMLElement>('[data-ov-card]')) {
            expect((card.firstElementChild as HTMLElement).className).toContain('bg-teal-sheet');
        }
        expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull();
    });

    it('a card press drags its cell by its index in the stack (O01)', async () => {
        const { container } = column({ id: 's', name: 'Options', cells });
        const old = container.querySelector('[data-ov-card="old"]') as HTMLElement;
        fireEvent.pointerDown(old, { button: 0 });
        expect(vi.mocked(callbacks.onCellDragStart!).mock.calls[0].slice(0, 2)).toEqual([1, 'old']);
    });

    const kidsOf = (container: HTMLElement) => [...container.querySelector('[data-ov-column]')!.children]
        .map(el => el.getAttribute('data-ov-card') ?? (el.hasAttribute('data-drop-slot') ? 'gap' : '?'));

    it('the marked gap renders at the pointed stack position: above the first card, between two, below the last (polish)', async () => {
        for (const [preview, kids] of [
            [0, ['gap', 'new', 'old', 'mid']], [1, ['new', 'gap', 'old', 'mid']], [3, ['new', 'old', 'mid', 'gap']],
        ] as const) {
            const { container } = column({ id: 's', name: 'Options', cells }, { preview });
            expect(kidsOf(container)).toEqual(kids);
            cleanup();
        }
        // The held card's own column shows the others, the position counted without it.
        const { container } = column({ id: 's', name: 'Options', cells }, { preview: 1, hidden: 'new' });
        expect(kidsOf(container)).toEqual(['old', 'gap', 'mid']);
    });

    it('Only connected keeps the connected cards; a drop opens a marked card-tall gap at its slot (polish)', async () => {
        // Stack position 2 (before Middle, which Only connected hides) is
        // below Oldest, the last shown card.
        const { container } = column({ id: 's', name: 'Options', cells },
            { board: { keep: new Set(['new', 'old']), journalCellId: null, followCellId: null }, preview: 2 });
        const list = container.querySelector('[data-ov-column]') as HTMLElement;
        expect(kidsOf(container)).toEqual(['new', 'old', 'gap']);
        const gap = list.children[2] as HTMLElement;
        expect(gap.style.height).toBe('273px');
        expect(gap.className).toContain('border-dashed');
        expect(gap.className).toContain('border-teal');
    });
});
