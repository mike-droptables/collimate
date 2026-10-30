import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import type { FocusContextType } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { AppState, Cell, VirtualFile } from '@/lib/types';

// F0546: the header's title editor sends only the name
// (PATCH /cells/{id}/meta), never the whole cached cell through
// onUpdate (update_top), whose file list can lag the server's.
vi.mock('./CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    patchCellMeta: vi.fn(async () => ({ tags: [] })),
}));

import * as api from '@/lib/api';
import { StackItem } from './StackItem';

const vf = (name: string, content: string): VirtualFile => (fileFixture(name, content, {language: 'plaintext', timestamp: 't'}));
const cell = (extra: Partial<Cell> = {}): Cell => ({
    id: 'cell-1', name: 'Old name', files: [vf('notes.md', 'stale cached body')], channel: 'ch', ...extra,
});

const focus = {
    focusArea: 'main', selectedWindowId: null, mainFocusMode: 'window',
    setFocusArea: () => {}, setSelectedWindowId: () => {}, setMainFocusMode: () => {},
} as unknown as FocusContextType;

type Props = React.ComponentProps<typeof StackItem>;
const mount = (over: Partial<Props> = {}) => {
    const onUpdate = vi.fn();
    render(<StackItem
        stackId="s1" cell={cell()} index={0} isFront isSelected viewMode="stack"
        width={600} height={400}
        focusArea={focus.focusArea} onSelect={vi.fn()} onUpdate={onUpdate}
        {...over}
    />);
    return { onUpdate };
};

const resetStore = () => useCollimateStore.setState({
    cellCache: {}, runs: {}, stoppingRunIds: new Set(),
    runPhases: {}, runLogs: {}, lastFinishedByCell: {},
    cellLayouts: {}, state: null, pendingSelectCellId: null,
} as never);

const rename = (to: string) => {
    fireEvent.click(screen.getByRole('button', { name: 'Old name' }));
    const input = screen.getByDisplayValue('Old name');
    fireEvent.change(input, { target: { value: to } });
    fireEvent.keyDown(input, { key: 'Enter' });
};

describe('StackItem header — name edits patch only the metadata (F0546, C11)', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        resetStore();
        vi.mocked(api.patchCellMeta).mockReset().mockResolvedValue(
            { id: 'cell-1', name: '', updated_at: '', tags: [] },
        );
    });

    it('a rename sends {name} and never the cached files (C11)', () => {
        const { onUpdate } = mount();
        rename('New name');
        expect(api.patchCellMeta).toHaveBeenCalledWith('ch', 'cell-1', { name: 'New name' });
        expect(onUpdate).not.toHaveBeenCalled();
        expect(useCollimateStore.getState().cellCache['cell-1']?.name).toBe('New name');
    });

    // The cache entry comes from the real applyServerState, so a rebroadcast
    // of the unchanged server cell (same updated_at) is the identity skip a
    // failed edit would otherwise survive.
    const serverState = (): AppState => ({
        active_channel_id: 'ch-1',
        channels: [{
            id: 'ch-1', name: 'ch', nav_stacks: [],
            main_stacks: [{ id: 's1', name: '', cells: [cell({ updated_at: '2026-09-28T10:00:00.000000' })] }],
        }],
    } as unknown as AppState);

    it('a failed patch puts the prior name back, and a rebroadcast keeps it there', async () => {
        const server = serverState();
        useCollimateStore.getState().applyServerState(server);
        vi.mocked(api.patchCellMeta).mockRejectedValue(new Error('boom'));
        const error = vi.spyOn(console, 'error').mockImplementation(() => {});
        mount();
        rename('New name');
        expect(useCollimateStore.getState().cellCache['cell-1']?.name).toBe('New name');
        await waitFor(() => expect(useCollimateStore.getState().cellCache['cell-1']?.name).toBe('Old name'));
        useCollimateStore.getState().applyServerState(server);
        expect(useCollimateStore.getState().cellCache['cell-1']?.name).toBe('Old name');
        expect(screen.getByRole('button', { name: 'Old name' })).toBeTruthy();
        error.mockRestore();
    });

    it('a failed patch leaves a later edit alone', async () => {
        useCollimateStore.getState().applyServerState(serverState());
        let fail: (e: Error) => void = () => {};
        vi.mocked(api.patchCellMeta)
            .mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }))
            .mockResolvedValueOnce({ id: 'cell-1', name: 'Second', updated_at: '', tags: [] });
        const error = vi.spyOn(console, 'error').mockImplementation(() => {});
        mount();
        rename('First');
        fireEvent.click(screen.getByRole('button', { name: 'First' }));
        const input = screen.getByDisplayValue('First');
        fireEvent.change(input, { target: { value: 'Second' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        fail(new Error('boom'));
        await waitFor(() => expect(error).toHaveBeenCalled());
        expect(useCollimateStore.getState().cellCache['cell-1']?.name).toBe('Second');
        error.mockRestore();
    });
});

describe('StackItem body clip reads the live radius token (S04)', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        resetStore();
    });

    it('rounds the body clip from var(--radius), never an unemitted tier variable (S04)', () => {
        mount();
        const clipped = Array.from(document.querySelectorAll<HTMLElement>('div'))
            .filter((el) => (el.getAttribute('style') ?? '').includes('clip-path'));
        expect(clipped).toHaveLength(1);
        const style = clipped[0].getAttribute('style') ?? '';
        expect(style).toContain('var(--radius)');
        expect(style).not.toContain('--radius-md');
    });
});

describe('StackItem selection visuals (C10)', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        resetStore();
    });
    const card = () => document.querySelector('[data-stack-card]') as HTMLElement;
    const classes = () => card().className.split(/\s+/);

    it('the selected cell carries a 1px brass border and no heavy shadow (C10)', () => {
        mount();
        expect(classes()).toEqual(expect.arrayContaining(['border', 'border-brass']));
        expect(classes()).not.toContain('nav-cursor');
        expect(card().className).not.toMatch(/\bshadow-(?:sm|md|lg|xl|2xl)\b/);
    });

    it('with focus in the nav area the selected cell keeps the quieter line-2 outline (C10)', () => {
        mount({ focusArea: 'nav' });
        expect(classes()).toEqual(expect.arrayContaining(['border-line', 'nav-cursor']));
        expect(classes()).not.toContain('border-brass');
    });

    it('any other cell sits on the hairline (C10)', () => {
        mount({ isSelected: false });
        expect(classes()).toContain('border-line');
        expect(classes()).not.toContain('border-brass');
        expect(classes()).not.toContain('nav-cursor');
        cleanup();
        mount({ isFront: false });
        expect(classes()).not.toContain('border-brass');
    });

    it('a selected Capabilities cell takes the same brass outline (C10)', () => {
        mount({ capabilitiesCell: true });
        expect(classes()).toContain('border-brass');
    });
});

describe('StackItem Capabilities styling (D-C05)', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        resetStore();
    });

    it('a Capabilities cell tints the front head teal without a decorative glyph (C05)', () => {
        mount({ capabilitiesCell: true });
        expect(document.querySelector('[data-stack-card] .bg-teal-sheet')).not.toBeNull();
        expect(screen.queryByRole('button', { name: /cell icon/i })).toBeNull();
        expect(document.querySelector('[data-stack-card] svg.lucide-monitor')).toBeNull();
    });

    it('a Capabilities cell renames like any cell: the name PATCH only (C11)', () => {
        vi.mocked(api.patchCellMeta).mockReset().mockResolvedValue(
            { id: 'cell-1', name: '', updated_at: '', tags: [] },
        );
        const { onUpdate } = mount({ capabilitiesCell: true });
        rename('Kit');
        expect(api.patchCellMeta).toHaveBeenLastCalledWith('ch', 'cell-1', { name: 'Kit' });
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('an ordinary stack keeps the plain head (C05)', () => {
        mount();
        expect(document.querySelector('[data-stack-card] .bg-teal-sheet')).toBeNull();
    });
});

describe('StackItem header title and run light (C11)', () => {
    beforeEach(() => {
        cleanup();
        localStorage.clear();
        resetStore();
    });

    it('the title is the editable name in the UI face, 14px/600; zoomed out it is the card\'s plain heading (C11) (O01)', () => {
        mount();
        const title = screen.getByRole('button', { name: 'Old name' });
        expect(title.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-[0.875rem]', 'font-semibold']));
        expect(title.className).not.toContain('font-mono');
        cleanup();
        mount({ viewMode: 'overview' });
        const heading = screen.getByRole('heading', { name: 'Old name' });
        expect(heading.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-[0.875rem]', 'font-semibold']));
        expect(screen.queryByRole('button', { name: 'Old name' })).toBeNull();
    });

    it('the rename field keeps the title\'s face (C11)', () => {
        mount();
        fireEvent.click(screen.getByRole('button', { name: 'Old name' }));
        const input = screen.getByDisplayValue('Old name');
        expect(input.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-[0.875rem]', 'font-semibold']));
    });

    it('a running cell places its light before the title, with no cell icon', () => {
        useCollimateStore.setState({
            runs: { r: { runId: 'r', cellId: 'cell-1', channel: 'ch', runnerId: 'h', runnerName: 'Synthetic', generatorName: 'g', label: 'h-11111111', folder: '_runs/h-11111111' } },
        } as never);
        mount({ cell: cell({ icon: 'box' }) });
        const light = screen.getByTestId('run-light');
        const title = screen.getByRole('button', { name: 'Old name' });
        expect(light.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(light.getAttribute('data-light-state')).toBe('running');
        expect(light.querySelector('svg')).toBeNull();
        expect(screen.queryByRole('button', { name: /cell icon/i })).toBeNull();
    });
});
