import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react';
import { StackGroup, type StackCallbacks } from './StackGroup';
import { FocusAreaContext } from '@/contexts/FocusContext';
import { ChannelReadOnlyContext } from '@/contexts/ChannelReadOnlyContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, Stack } from '@/lib/types';

// An archived channel's stack menu keeps only what reads.
vi.mock('./StackItem', () => ({ StackItem: () => <div data-testid="stack-item" /> }));
vi.mock('./capabilities/CapabilitiesManageCard', () => ({ CapabilitiesManageCard: () => <div data-testid="manage-card" /> }));

const cell = (id: string): Cell => ({ id, name: id, files: [] });
const twoCells: Stack = { id: 's1', name: '', cells: [cell('a'), cell('b')] };

const callbacks = (over: Partial<StackCallbacks> = {}): StackCallbacks => ({
    onSelect: () => {}, onClose: () => {}, onRemoveCell: () => {}, onUpdate: () => {},
    onRenameStack: vi.fn(), onFanOut: vi.fn(), onDuplicateStack: vi.fn(), onMergeStack: vi.fn(), ...over,
});

const mount = (readOnly: boolean | undefined, cb: StackCallbacks, stackIndex = 1, stackCount = 3) => {
    const group = <FocusAreaContext.Provider value="main">
        <StackGroup stack={twoCells} width={800} height={400} isSelected viewMode="stack" callbacks={cb}
            stackIndex={stackIndex} stackCount={stackCount} visibleTopIndex={0} />
    </FocusAreaContext.Provider>;
    render(readOnly === undefined ? group
        : <ChannelReadOnlyContext.Provider value={readOnly}>{group}</ChannelReadOnlyContext.Provider>);
};
const menuLabels = () => {
    fireEvent.click(screen.getByRole('button', { name: 'Stack menu' }));
    return within(screen.getByRole('menu')).queryAllByRole('menuitem').map(el => (el.textContent ?? '').trim());
};

describe('StackGroup in an archived channel', () => {
    beforeEach(() => {
        cleanup();
        useCollimateStore.setState({ runs: {}, stoppingRunIds: new Set(), runPhases: {} } as never);
    });

    it('without the context the stack menu carries Rename, Duplicate, Merge and Remove (plan 1105 P7)', () => {
        mount(undefined, callbacks());
        const labels = menuLabels().join('|');
        for (const row of ['Rename', 'Fan out', 'Duplicate', 'Merge', 'Remove']) expect(labels).toContain(row);
    });

    it('with the context true the menu keeps only the Fan out row, no separators (plan 1105 P7)', () => {
        mount(true, callbacks());
        const labels = menuLabels();
        expect(labels).toHaveLength(1);
        expect(labels[0]).toMatch(/^Fan out/);
        expect(within(screen.getByRole('menu')).queryAllByRole('separator')).toHaveLength(0);
        expect(screen.queryByRole('menuitem', { name: /^(Rename|Duplicate|Merge|Remove)/ })).toBeNull();
    });

    it('the head\'s move arrows are enabled only when onMoveStack is given (plan 1105 P7)', () => {
        mount(false, callbacks({ onMoveStack: vi.fn() }));
        expect((screen.getByRole('button', { name: 'Move stack left' }) as HTMLButtonElement).disabled).toBe(false);
        expect((screen.getByRole('button', { name: 'Move stack right' }) as HTMLButtonElement).disabled).toBe(false);
        cleanup();
        mount(true, callbacks());
        expect((screen.getByRole('button', { name: 'Move stack left' }) as HTMLButtonElement).disabled).toBe(true);
        expect((screen.getByRole('button', { name: 'Move stack right' }) as HTMLButtonElement).disabled).toBe(true);
    });
});
