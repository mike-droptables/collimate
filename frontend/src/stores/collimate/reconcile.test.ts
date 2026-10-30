import { describe, it, expect } from 'vitest';
import { reconcileAppState, reconcileChannel, reconcileStack } from './reconcile';
import { AppState, Channel, Stack, Cell } from '@/lib/types';

const cell = (id: string, updated_at = 't1', extra: Partial<Cell> = {}): Cell => ({
    id,
    name: `cell ${id}`,
    files: [],
    updated_at,
    ...extra,
});

const stack = (id: string, cells: Cell[], extra: Partial<Stack> = {}): Stack => ({
    id,
    name: `stack ${id}`,
    cells,
    ...extra,
});

const channel = (id: string, main: Stack[], extra: Partial<Channel> = {}): Channel => ({
    id,
    name: `channel ${id}`,
    main_stacks: main,
    nav_stacks: [],
    nav_selection_id: null,
    main_selection_id: null,
    ...extra,
});

const appState = (channels: Channel[], extra: Partial<AppState> = {}): AppState => ({
    channels,
    active_channel_id: channels[0]?.id ?? null,
    ...extra,
});

// Fresh-parse clone, like an SSE payload: same values, all-new identities.
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));

describe('reconcileStack', () => {
    it('returns the previous stack when nothing changed', () => {
        const prev = stack('s1', [cell('c1'), cell('c2')]);
        expect(reconcileStack(prev, clone(prev))).toBe(prev);
    });

    it('reuses unchanged cell identities inside a changed stack', () => {
        const c1 = cell('c1');
        const prev = stack('s1', [c1, cell('c2', 't1')]);
        const next = clone(prev);
        next.cells[1].updated_at = 't2';
        const out = reconcileStack(prev, next);
        expect(out).not.toBe(prev);
        expect(out.cells[0]).toBe(c1);
        expect(out.cells[1]).toBe(next.cells[1]);
    });

    it('reuses moved cells by id on reorder, but not the array', () => {
        const c1 = cell('c1');
        const c2 = cell('c2');
        const prev = stack('s1', [c1, c2]);
        const next = clone(prev);
        next.cells.reverse();
        const out = reconcileStack(prev, next);
        expect(out).not.toBe(prev);
        expect(out.cells[0]).toBe(c2);
        expect(out.cells[1]).toBe(c1);
    });

    it('does not reuse a cell without updated_at', () => {
        const prev = stack('s1', [cell('c1', undefined as unknown as string)]);
        delete prev.cells[0].updated_at;
        const next = clone(prev);
        const out = reconcileStack(prev, next);
        expect(out.cells[0]).toBe(next.cells[0]);
    });

    it('detects own-field changes (rename)', () => {
        const prev = stack('s1', [cell('c1')]);
        const next = clone(prev);
        next.name = 'renamed';
        const out = reconcileStack(prev, next);
        expect(out).not.toBe(prev);
        expect(out.name).toBe('renamed');
        expect(out.cells).toBe(prev.cells);
    });
});

describe('reconcileChannel', () => {
    it('returns the previous channel when nothing changed', () => {
        const prev = channel('ch1', [stack('s1', [cell('c1')])]);
        expect(reconcileChannel(prev, clone(prev))).toBe(prev);
    });

    it('reuses untouched stacks when one stack changed', () => {
        const s1 = stack('s1', [cell('c1')]);
        const s2 = stack('s2', [cell('c2')]);
        const prev = channel('ch1', [s1, s2]);
        const next = clone(prev);
        next.main_stacks[1].cells[0].updated_at = 't2';
        const out = reconcileChannel(prev, next);
        expect(out).not.toBe(prev);
        expect(out.main_stacks[0]).toBe(s1);
        expect(out.main_stacks[1]).not.toBe(s2);
    });

    it('reuses moved stacks by id on reorder', () => {
        const s1 = stack('s1', [cell('c1')]);
        const s2 = stack('s2', [cell('c2')]);
        const prev = channel('ch1', [s1, s2]);
        const next = clone(prev);
        next.main_stacks.reverse();
        const out = reconcileChannel(prev, next);
        expect(out.main_stacks[0]).toBe(s2);
        expect(out.main_stacks[1]).toBe(s1);
    });

    it('detects selection changes', () => {
        const prev = channel('ch1', [stack('s1', [cell('c1')])]);
        const next = clone(prev);
        next.main_selection_id = 's1';
        const out = reconcileChannel(prev, next);
        expect(out).not.toBe(prev);
        expect(out.main_stacks).toBe(prev.main_stacks);
    });
});

describe('reconcileAppState', () => {
    it('returns the previous state for an identical push', () => {
        const prev = appState([channel('ch1', [stack('s1', [cell('c1')])])]);
        expect(reconcileAppState(prev, clone(prev))).toBe(prev);
    });

    it('returns next verbatim when there is no previous state', () => {
        const next = appState([channel('ch1', [])]);
        expect(reconcileAppState(null, next)).toBe(next);
    });

    it('reuses untouched channels when another channel changed', () => {
        const ch1 = channel('ch1', [stack('s1', [cell('c1')])]);
        const ch2 = channel('ch2', [stack('s2', [cell('c2')])]);
        const prev = appState([ch1, ch2]);
        const next = clone(prev);
        next.channels[1].main_stacks[0].cells[0].updated_at = 't9';
        const out = reconcileAppState(prev, next);
        expect(out).not.toBe(prev);
        expect(out.channels[0]).toBe(ch1);
        expect(out.channels[1]).not.toBe(ch2);
    });

    it('added and removed cells produce fresh stack identity but keep survivors', () => {
        const c1 = cell('c1');
        const prev = appState([channel('ch1', [stack('s1', [c1])])]);
        const next = clone(prev);
        next.channels[0].main_stacks[0].cells.push(cell('c2'));
        const out = reconcileAppState(prev, next);
        const outStack = out.channels[0].main_stacks[0];
        expect(outStack).not.toBe(prev.channels[0].main_stacks[0]);
        expect(outStack.cells[0]).toBe(c1);
        expect(outStack.cells).toHaveLength(2);
    });
});
