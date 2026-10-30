import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell } from '@/lib/types';

// TG-05: the main-view card's Tags button sits immediately left of the
// "Move cell" grip, on every cell but a built-in card (Capabilities cells
// included), and nothing in its menu selects the card.
vi.mock('./CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));
vi.mock('@/lib/api/cells', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/cells')>()),
    patchCellMeta: vi.fn(async (_channel: string, _cellId: string, meta: { tags?: string[] }) => ({ tags: meta.tags ?? [] })),
    fetchChannelTags: vi.fn(async () => [{ tag: 'later', live: 0, removed: 1 }]),
}));

import * as cells from '@/lib/api/cells';
import { StackItem } from './StackItem';

const cell = (extra: Partial<Cell> = {}): Cell => ({ id: 'cell-1', name: 'Notes', files: [], channel: 'ch', tags: ['draft'], ...extra });

type Props = React.ComponentProps<typeof StackItem>;
const mount = (over: Partial<Props> = {}) => {
    const onSelect = vi.fn();
    render(<StackItem
        stackId="s1" cell={cell()} index={0} isFront isSelected={false} viewMode="stack"
        width={600} height={400} focusArea="main" onSelect={onSelect} onUpdate={vi.fn()}
        onCellDragStart={vi.fn()} onDuplicateToNewStack={vi.fn()} onRemoveCell={vi.fn()}
        {...over}
    />);
    return { onSelect };
};

const initial = useCollimateStore.getState();

describe('StackItem: the Tags button (TG-05)', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        useCollimateStore.setState(initial, true);
        vi.mocked(cells.patchCellMeta).mockClear();
    });

    it('sits immediately left of the Move grip, with the cell\'s count (TG-05)', () => {
        mount();
        const tools = screen.getAllByRole('button').filter(b => b.closest('[data-stack-card]'));
        const at = tools.findIndex(b => b.getAttribute('aria-label') === 'Tags');
        expect(at).toBeGreaterThanOrEqual(0);
        expect(tools[at + 1].getAttribute('aria-label')).toBe('Move cell');
        expect(tools[at].textContent).toBe('1');
    });

    it('a Capabilities cell carries it too, left of the Move grip (TG-05) (D21)', () => {
        mount({ capabilitiesCell: true, onHideStack: vi.fn() });
        const tools = screen.getAllByRole('button').filter(b => b.closest('[data-stack-card]'));
        const at = tools.findIndex(b => b.getAttribute('aria-label') === 'Tags');
        expect(at).toBeGreaterThanOrEqual(0);
        expect(tools[at + 1].getAttribute('aria-label')).toBe('Move cell');
        expect(tools[at].textContent).toBe('1');
    });

    it('is absent on a built-in card, which takes no metadata (TG-05)', () => {
        mount({ capabilitiesCell: true, cell: cell({ name: 'Definitions', builtin: true }) });
        expect(screen.queryByRole('button', { name: 'Tags' })).toBeNull();
    });

    it('opening it and working in the menu never selects the card (TG-05)', async () => {
        const { onSelect } = mount();
        const trigger = screen.getByRole('button', { name: 'Tags' });
        fireEvent.pointerDown(trigger, { button: 0 });
        fireEvent.click(trigger);
        const dialog = await screen.findByRole('dialog', { name: 'Tags' });
        const later = await screen.findByRole('button', { name: 'later' });
        fireEvent.pointerDown(dialog, { button: 0 });
        fireEvent.pointerDown(later, { button: 0 });
        fireEvent.click(later);
        expect(cells.patchCellMeta).toHaveBeenCalledWith('ch', 'cell-1', { tags: ['draft', 'later'] });
        expect(onSelect).not.toHaveBeenCalled();
        expect(screen.getByRole('button', { name: 'Tags' }).textContent).toBe('2');
    });
});
