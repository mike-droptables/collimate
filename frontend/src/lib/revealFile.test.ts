import { describe, expect, it, beforeEach } from 'vitest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { activeTab } from '@/lib/cellLayout';
import { fileFixture } from '@/test/fileFixtures';
import type { Cell } from '@/lib/types';
import { revealCellFile, revealHandler } from './revealFile';

const cell = { id: 'cell', name: 'Memo', files: [fileFixture('memo/memo.handler', 'id: memo'), fileFixture('notes.md', '# n')] } as Cell;

beforeEach(() => {
    const channel = { id: 'ch', name: 'ch', nav_stacks: [], nav_selection_id: null, main_selection_id: null,
        main_stacks: [{ id: 'k', name: 'Capabilities', stack_type: 'capabilities' as const, cells: [cell] }] };
    useCollimateStore.setState({
        state: { active_channel_id: 'ch', channels: [channel] } as never, activeChannel: channel as never,
        cellCache: {}, cellLayouts: {}, capabilitiesManageRequest: null, pendingSelectCellId: null, pendingSelectFocus: false,
    } as never);
});

describe('revealCellFile', () => {
    it('selects the cell and opens the file in a tab', () => {
        revealCellFile('cell', 'notes.md', { newTab: true });
        const state = useCollimateStore.getState();
        expect(state.pendingSelectCellId).toBe('cell');
        expect(state.pendingSelectFocus).toBe(false);
        const tab = activeTab(state.cellLayouts.cell);
        expect(tab?.kind === 'file' && tab.path).toBe('notes.md');
    });

    it('with focus, also asks for the main focus area (an annotation pick)', () => {
        revealCellFile('cell', 'notes.md', { newTab: true, focus: true });
        expect(useCollimateStore.getState().pendingSelectFocus).toBe(true);
        useCollimateStore.getState().clearPendingSelectCell();
        const state = useCollimateStore.getState();
        expect([state.pendingSelectCellId, state.pendingSelectFocus]).toEqual([null, false]);
    });

    it('does nothing for a path the cell does not hold', () => {
        revealCellFile('cell', 'gone.md');
        expect(useCollimateStore.getState().pendingSelectCellId).toBeNull();
    });
});

describe('revealHandler', () => {
    it('asks the Capabilities manage card for the handler card and its step', () => {
        revealHandler('cell', 'memo', 'review');
        const state = useCollimateStore.getState();
        const request = state.capabilitiesManageRequest;
        expect(request).toMatchObject({ kind: 'handler', handler: { cellId: 'cell', folder: 'memo', step: 'review' } });
        // It never selects the cell or touches its layout: the manage card shows it.
        expect(state.pendingSelectCellId).toBeNull();
        expect(state.cellLayouts.cell).toBeUndefined();
        state.clearCapabilitiesManageRequest(request!.seq);
        expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull();
    });

    it('omits the step when none is asked for', () => {
        revealHandler('cell', 'memo');
        expect(useCollimateStore.getState().capabilitiesManageRequest?.handler).toEqual({ cellId: 'cell', folder: 'memo' });
    });
});
