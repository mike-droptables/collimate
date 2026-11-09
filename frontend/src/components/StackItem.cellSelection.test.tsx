import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import type { Cell } from '@/lib/types';

// The cell editor stands in as a reader of the selection context, the way
// the tab rule, the files rail and the pin read it.
vi.mock('./CellEditor', () => ({
    default: function Reader() {
        return <div data-testid="cell-editor" data-selected={String(useCellSelected())} />;
    },
}));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));

import { StackItem } from './StackItem';

const cell: Cell = { id: 'cell-1', name: 'A cell', files: [], channel: 'ch' };
type Props = React.ComponentProps<typeof StackItem>;
const mount = (over: Partial<Props>) => render(<StackItem
    stackId="s1" cell={cell} index={0} isFront isSelected viewMode="stack"
    width={600} height={400}
    focusArea="main" onSelect={vi.fn()} onUpdate={vi.fn()}
    {...over}
/>);
const selected = () => screen.getByTestId('cell-editor').getAttribute('data-selected');

describe('StackItem provides CellSelectionContext around its editor', () => {
    beforeEach(() => {
        cleanup();
        useCollimateStore.setState({
            cellCache: {}, runs: {}, stoppingRunIds: new Set(), runPhases: {}, runLogs: {},
            cellLayouts: {}, state: null, pendingSelectCellId: null,
        } as never);
    });

    it('true for the selected stack\'s front cell, focused or not (C10)', () => {
        mount({ isSelected: true, focusArea: 'main' });
        expect(selected()).toBe('true');
        cleanup();
        mount({ isSelected: true, focusArea: 'nav' });
        expect(selected()).toBe('true');
    });

    it('false in an unselected stack (C10)', () => {
        mount({ isSelected: false });
        expect(selected()).toBe('false');
    });
});
