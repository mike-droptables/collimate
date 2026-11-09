import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, CellRepository } from '@/lib/types';

vi.mock('./CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));

import { StackItem } from './StackItem';

const repo = (path: string, patch: Partial<CellRepository> = {}): CellRepository =>
    ({ path, branch: 'main', upstream: null, ahead: null, behind: null, detached: false, ...patch });
const cell = (repositories: CellRepository[]): Cell => ({ id: 'cell-1', name: 'Repo', files: [], channel: 'ch', repositories });
const mount = (c: Cell) => render(<StackItem
    stackId="s1" cell={c} index={0} isFront isSelected viewMode="stack"
    width={600} height={400} focusArea="main" onSelect={vi.fn()} onUpdate={vi.fn()}
    onCellDragStart={vi.fn()} onDuplicateToNewStack={vi.fn()} onRemoveCell={vi.fn()}
/>);
const initial = useCollimateStore.getState();

// A root repository's chip sits on the cell head after the title, as
// BuiltinChip does: never between the run light and Run (§11).
describe('StackItem: the root repository’s chip', () => {
    beforeEach(() => { cleanup(); useCollimateStore.setState(initial, true); });

    it('follows the title, past the status toggle\'s pill, and is flex-none at most 10rem', () => {
        mount(cell([repo('', { ahead: 1 }), repo('vendor', { branch: 'dev' })]));
        const chips = screen.getAllByTestId('repo-chip');
        expect(chips).toHaveLength(1);
        expect(chips[0]).toHaveTextContent('main↑1');
        expect(chips[0].className).toContain('flex-none');
        expect(chips[0].className).toContain('max-w-[10rem]');
        const run = screen.getByTestId('cell-status-toggle');
        expect(run.compareDocumentPosition(chips[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(screen.getByText('Repo').compareDocumentPosition(chips[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it('a cell without a root repository has no head chip', () => {
        mount(cell([repo('vendor')]));
        expect(screen.queryByTestId('repo-chip')).toBeNull();
    });
});
