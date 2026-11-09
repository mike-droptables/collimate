import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell } from '@/lib/types';

vi.mock('./CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));
const pool = vi.hoisted(() => ({
    untrack: vi.fn(),
    registerMotionLayer: vi.fn(),
}));
vi.mock('@/contexts/GuestPoolContext', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/contexts/GuestPoolContext')>();
    return {
        ...actual,
        useGuestPool: () => ({
            ...actual.useGuestPool(),
            registerMotionLayer: pool.registerMotionLayer,
        }),
    };
});

import { StackItem } from './StackItem';

const cell: Cell = { id: 'cell-1', name: 'A cell', files: [], channel: 'ch' };
type Props = React.ComponentProps<typeof StackItem>;
const ui = (over: Partial<Props> = {}) => (
    <StackItem
        stackId="s1" cell={cell} index={0} isFront isSelected viewMode="stack"
        width={600} height={400}
        focusArea="main" onSelect={vi.fn()} onUpdate={vi.fn()}
        {...over}
    />
);
const card = () => document.querySelector<HTMLElement>('[data-stack-card]')!;
const shell = () => document.querySelector<HTMLElement>('[data-cell-id]')!;
const PROMOTION = /will-change|transform-gpu/;

describe('the stack card is promoted only while it moves', () => {
    beforeEach(() => {
        pool.untrack.mockClear();
        pool.registerMotionLayer.mockReset().mockImplementation(() => pool.untrack);
        useCollimateStore.setState({
            cellCache: {}, runs: {}, stoppingRunIds: new Set(), runPhases: {}, runLogs: {},
            cellLayouts: {}, state: null, pendingSelectCellId: null,
        } as never);
    });
    afterEach(cleanup);

    it('registers the card as a motion layer, carries no promotion at rest, and untracks it on unmount', () => {
        const view = render(ui());
        expect(pool.registerMotionLayer).toHaveBeenCalledOnce();
        expect(pool.registerMotionLayer).toHaveBeenCalledWith(card());
        expect(card().className).not.toMatch(PROMOTION);
        expect(shell().className).not.toMatch(PROMOTION);
        expect(shell().style.willChange).toBe('');
        view.unmount();
        expect(pool.untrack).toHaveBeenCalledOnce();
    });

    it('promotes its shell from the start of its own animation, before a frame, to its end', async () => {
        const onTransitionChange = vi.fn();
        const view = render(ui({ onTransitionChange, isRecentlyDropped: true }));
        expect(shell().style.willChange).toBe('');
        act(() => view.rerender(ui({ onTransitionChange, isRecentlyDropped: true, height: 300 })));
        expect(shell().style.willChange).toBe('transform, opacity');
        expect(onTransitionChange).toHaveBeenLastCalledWith('cell-1', true);
        await waitFor(() => expect(onTransitionChange).toHaveBeenLastCalledWith('cell-1', false));
        expect(shell().style.willChange).toBe('');
        expect(card().className).not.toMatch(PROMOTION);
    });
});
