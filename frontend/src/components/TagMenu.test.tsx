import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { Cell } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';

vi.mock('@/lib/api/cells', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/cells')>()),
    patchCellMeta: vi.fn(),
    fetchChannelTags: vi.fn(),
}));

import * as cells from '@/lib/api/cells';
import { CellTagMenu } from './TagMenu';
import { TAGS_PER_CELL_MAX } from '@/lib/tags';

// TG-05: the tag menu. A Tag button with a count opens a Menu dialog: the
// search, this cell's chips, the channel's tags to toggle and Create.

const initial = useCollimateStore.getState();
const cell = (tags: string[] = ['draft']): Cell => ({ id: 'cell-1', name: 'Notes', files: [], channel: 'ch', tags });

const mount = (c: Cell = cell()) => {
    const cardClick = vi.fn();
    const cardDown = vi.fn();
    render(
        <div data-testid="card" onClick={cardClick} onPointerDown={cardDown}>
            <CellTagMenu channel="ch" cell={c} />
        </div>,
    );
    return { cardClick, cardDown };
};
const trigger = () => screen.getByRole('button', { name: 'Tags' });
const open = async (c?: Cell) => {
    mount(c);
    fireEvent.click(trigger());
    const dialog = await screen.findByRole('dialog', { name: 'Tags' });
    await waitFor(() => expect(cells.fetchChannelTags).toHaveBeenCalledWith('ch'));
    await screen.findByRole('button', { name: 'needs review' });
    return dialog;
};
const search = () => screen.getByRole('textbox', { name: 'Search or create a tag' });
const type = (text: string) => fireEvent.change(search(), { target: { value: text } });
const lastSent = () => vi.mocked(cells.patchCellMeta).mock.lastCall?.[2];
const cachedTags = () => useCollimateStore.getState().cellCache['cell-1']?.tags;

describe('TagMenu (TG-05)', () => {
    beforeEach(() => {
        useCollimateStore.setState(initial, true);
        vi.mocked(cells.patchCellMeta).mockReset().mockImplementation(async (_c, id, meta) => ({ id, tags: meta.tags ?? [] }));
        vi.mocked(cells.fetchChannelTags).mockReset().mockResolvedValue([
            { tag: 'draft', live: 1, removed: 0 },
            { tag: 'needs review', live: 0, removed: 2 },
            { tag: 'Reviewed', live: 1, removed: 0 },
        ]);
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    it('the trigger is a Tags button with the count past zero (TG-05)', () => {
        mount(cell(['a', 'b']));
        expect(trigger().getAttribute('title')).toBe('Tags');
        expect(trigger().textContent).toBe('2');
        expect(trigger().querySelector('svg.lucide-tag')).not.toBeNull();
        cleanup();
        mount(cell([]));
        expect(trigger().textContent).toBe('');
    });

    it('opens a dialog: the search focused first, then the chips, then the channel\'s tags (TG-05)', async () => {
        const dialog = await open();
        expect(document.activeElement).toBe(search());
        expect(search().getAttribute('placeholder')).toBe('Search or create a tag');
        const chips = within(dialog).getByRole('list', { name: 'This cell\'s tags' });
        expect(within(chips).getByText('draft')).toBeTruthy();
        const order = [search(), dialog.querySelector('[role="separator"]')!, chips, screen.getByRole('button', { name: 'needs review' })];
        for (let i = 1; i < order.length; i++) {
            expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        }
        // The cell's own tag is a pressed toggle; the others are not.
        expect(screen.getByRole('button', { name: 'draft' }).getAttribute('aria-pressed')).toBe('true');
        expect(screen.getByRole('button', { name: 'needs review' }).getAttribute('aria-pressed')).toBe('false');
    });

    it('the search narrows the channel\'s tags by casefold substring (TG-05)', async () => {
        await open();
        type('REV');
        expect(screen.getByRole('button', { name: 'needs review' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Reviewed' })).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'draft' })).toBeNull();
    });

    it('a row toggles its tag on and off the cell (TG-05)', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'needs review' }));
        expect(lastSent()).toEqual({ tags: ['draft', 'needs review'] });
        expect(cachedTags()).toEqual(['draft', 'needs review']);
        expect(trigger().textContent).toBe('2');
        fireEvent.click(screen.getByRole('button', { name: 'draft' }));
        expect(lastSent()).toEqual({ tags: ['needs review'] });
    });

    it('Create appears without an exact casefold match and adds what was typed (TG-05)', async () => {
        await open();
        type('reviewed');
        expect(screen.queryByRole('button', { name: /^Create/ })).toBeNull();
        type('  brand   new ');
        fireEvent.click(screen.getByRole('button', { name: 'Create “brand new”' }));
        expect(lastSent()).toEqual({ tags: ['draft', 'brand new'] });
        expect((search() as HTMLInputElement).value).toBe('');
    });

    it('a chip\'s x takes its tag off (TG-05)', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Remove draft' }));
        expect(lastSent()).toEqual({ tags: [] });
        expect(screen.getByText('No tags yet')).toBeTruthy();
    });

    it('Enter adds the first match the cell lacks, else creates, and clears the box (TG-05)', async () => {
        await open();
        type('rev');
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(lastSent()).toEqual({ tags: ['draft', 'needs review'] });
        expect((search() as HTMLInputElement).value).toBe('');
        type('ship it');
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(lastSent()).toEqual({ tags: ['draft', 'needs review', 'ship it'] });
    });

    it('Enter on an exact match the cell already holds adds nothing else (TG-05)', async () => {
        vi.mocked(cells.fetchChannelTags).mockResolvedValue([
            { tag: 'draft', live: 1, removed: 0 },
            { tag: 'drafts', live: 0, removed: 1 },
            { tag: 'needs review', live: 0, removed: 2 },
        ]);
        await open();
        await screen.findByRole('button', { name: 'drafts' });
        type('DRAFT');
        expect(screen.queryByRole('button', { name: /Create/ })).toBeNull();
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(cells.patchCellMeta).not.toHaveBeenCalled();
        expect(cachedTags()).toBeUndefined();
        expect((search() as HTMLInputElement).value).toBe('');
        type('draft');
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(lastSent()).toEqual({ tags: ['draft', 'drafts'] });
    });

    it('the arrows pick another row for Enter (TG-05)', async () => {
        await open();
        type('rev');
        fireEvent.keyDown(search(), { key: 'ArrowDown' });
        fireEvent.keyDown(search(), { key: 'Enter' });
        expect(lastSent()).toEqual({ tags: ['draft', 'Reviewed'] });
    });

    it('Esc closes and hands the keyboard back to the trigger (TG-05)', async () => {
        await open();
        fireEvent.keyDown(search(), { key: 'Escape' });
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(document.activeElement).toBe(trigger());
    });

    it('no click or press inside reaches the card (TG-05)', async () => {
        const { cardClick, cardDown } = mount();
        fireEvent.pointerDown(trigger(), { button: 0 });
        fireEvent.click(trigger());
        const dialog = await screen.findByRole('dialog', { name: 'Tags' });
        await screen.findByRole('button', { name: 'needs review' });
        for (const el of [dialog, search(), within(dialog).getByRole('list')]) {
            fireEvent.pointerDown(el, { button: 0 });
            fireEvent.click(el);
        }
        fireEvent.click(screen.getByRole('button', { name: 'needs review' }));
        fireEvent.click(screen.getByRole('button', { name: 'Remove draft' }));
        expect(cardClick).not.toHaveBeenCalled();
        expect(cardDown).not.toHaveBeenCalled();
    });

    it('a refused save puts the chips back (TG-05)', async () => {
        vi.mocked(cells.patchCellMeta).mockRejectedValue(new Error('refused'));
        const error = vi.spyOn(console, 'error').mockImplementation(() => {});
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'needs review' }));
        expect(trigger().textContent).toBe('2');
        await waitFor(() => expect(trigger().textContent).toBe('1'));
        expect(cachedTags()).toEqual(['draft']);
        expect(error).toHaveBeenCalled();
    });

    it('a full cell offers no Create and no adds (TG-05)', async () => {
        const full = Array.from({ length: TAGS_PER_CELL_MAX }, (_, i) => `t${i}`);
        mount(cell(full));
        fireEvent.click(trigger());
        await screen.findByRole('button', { name: 'needs review' });
        type('fresh');
        expect(screen.queryByRole('button', { name: /^Create/ })).toBeNull();
        expect(screen.getByText(`A cell holds at most ${TAGS_PER_CELL_MAX} tags`)).toBeTruthy();
        type('');
        const row = screen.getByRole('button', { name: 'needs review' });
        expect(row.getAttribute('aria-disabled')).toBe('true');
        act(() => { fireEvent.click(row); });
        expect(cells.patchCellMeta).not.toHaveBeenCalled();
    });

    it.each([['shown', 'main_stacks'], ['hidden', 'nav_stacks']] as const)(
        'the vocabulary takes every stack\'s tags, Capabilities %s included (TG-05)', async (_label, where) => {
            const capabilities = {
                id: 'cap', name: 'Capabilities', stack_type: 'capabilities' as const,
                cells: [{ id: 'cap-cell', name: 'Kit', files: [], tags: ['toolkit'] }],
            };
            const board = { id: 's1', name: '', cells: [{ id: 'b1', name: 'B', files: [], tags: ['board-only'] }] };
            useCollimateStore.setState({
                activeChannel: {
                    id: 'ch', name: 'ch', main_selection_id: null, nav_selection_id: null,
                    main_stacks: where === 'main_stacks' ? [board, capabilities] : [board],
                    nav_stacks: where === 'nav_stacks' ? [capabilities] : [],
                },
            });
            await open();
            expect(screen.getByRole('button', { name: 'toolkit' })).toBeTruthy();
            expect(screen.getByRole('button', { name: 'board-only' })).toBeTruthy();
        });
});
