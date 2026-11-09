import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell } from '@/lib/types';

// The burn cover (F1898/F1905): while a removed cell crumbles, an opaque
// panel covers the live renderer in the card's content region, so what
// distorts is a solid shape; it takes no pointer events, so a click already
// on its way still reaches the cell editor. A removal that fails (resolves
// null) takes the cover away again.
vi.mock('./CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));

import { StackItem } from './StackItem';

const cell: Cell = { id: 'cell-1', name: 'Leaving', files: [fileFixture('notes.md', 'body', { language: 'plaintext' })], channel: 'ch' };

describe('StackItem removal cover (F1898, F1905)', () => {
    beforeEach(() => {
        cleanup();
        useCollimateStore.setState({
            cellCache: {}, runs: {}, stoppingRunIds: new Set(), runPhases: {}, runLogs: {},
            lastFinishedByCell: {}, cellLayouts: {}, state: null, pendingSelectCellId: null,
        } as never);
    });

    it('covers the content region while the cell leaves, passes clicks through, and lifts when the removal fails', async () => {
        let settle: (value: unknown) => void = () => {};
        const onRemoveCell = vi.fn(() => new Promise(resolve => { settle = resolve; }));
        render(<StackItem stackId="s1" cell={cell} index={0} isFront isSelected viewMode="stack" width={600} height={400}
            focusArea="main" onSelect={vi.fn()} onUpdate={vi.fn()} onRemoveCell={onRemoveCell} />);
        const region = (await screen.findByTestId('cell-editor')).closest('.flex-1.overflow-hidden.relative') as HTMLElement;
        expect(region).not.toBeNull();
        const cover = () => [...region.children].find(child => child.classList.contains('bg-sheet')) ?? null;
        expect(cover()).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Remove cell' }));
        expect(onRemoveCell).toHaveBeenCalledTimes(1);
        expect(cover()).not.toBeNull();
        expect([...cover()!.classList].sort()).toEqual(['absolute', 'bg-sheet', 'inset-0', 'pointer-events-none']);
        // A sibling of the editor's wrapper, painted after it: over the renderer, inside the region.
        expect(region.lastElementChild).toBe(cover());
        expect(region.contains(screen.getByTestId('cell-editor'))).toBe(true);

        settle(null);
        await waitFor(() => expect(cover()).toBeNull());
    });
});
