// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { JournalEvent, RemovedCell } from '@/lib/api/cells';

const history = vi.hoisted(() => ({ fetchCellHistory: vi.fn() }));
vi.mock('@/lib/api/history', async (original) => ({ ...await original<typeof import('@/lib/api/history')>(), ...history }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { RemovedCard } from './RemovedCard';

/** A local wall-clock time as the route sends it (UTC ISO). */
const at = (day: number, h: number, m: number) => new Date(2026, 9, day, h, m).toISOString();

const removed = (over: Partial<RemovedCell> = {}): RemovedCell => ({
    cell_id: 'g1', tip_version_id: 'g1-v2', name: 'Merge on conflict', icon: null, stack_name: null, stack_cell_count: 1,
    reason: 'removed', created_at: at(2, 9, 12), edited_at: at(5, 8, 14), removed_at: at(6, 10, 0),
    made_by: { kind: 'user', runner_id: null, runner_name: null },
    files: ['SeedIdea.md', 'merge-screen.html', 'questions.md'], file_count: 3,
    preview: { path: 'SeedIdea.md', text: '# Last write wins, merge screen on conflict' }, tags: [], ...over,
});

const card = (over: Partial<Parameters<typeof RemovedCard>[0]> = {}) => {
    const props = {
        entry: removed(), sort: 'edited' as const, selected: false, journalStyle: false, followHint: false,
        onClick: vi.fn(), onRestore: vi.fn(), ...over,
    };
    render(<RemovedCard {...props} />);
    return props;
};

beforeEach(() => {
    vi.clearAllMocks();
    useCollimateStore.setState(prev => ({ overviewData: { ...prev.overviewData, channel: 'chan', journals: {} } }));
});

describe('RemovedCard', () => {
    it('is a ghost: the edit time in local time, the author, the files and the preview (O05)', () => {
        card();
        const el = document.querySelector('[data-rm-card="g1"]')!;
        expect(el.className).toContain('border-dashed');
        expect(el.className).toContain('bg-canvas');
        expect(el).toHaveTextContent('Edited 5 Oct · 08:14');
        expect(el).toHaveTextContent('you');
        expect(el).toHaveTextContent('Files (3)');
        expect(el).toHaveTextContent('merge-screen.html');
        expect(el).toHaveTextContent('questions.md');
        expect(document.querySelector('[data-rm-preview-name]')).toHaveTextContent('SeedIdea.md');
        expect(el.querySelector('pre')).toHaveTextContent('# Last write wins, merge screen on conflict');
        expect(screen.queryByRole('button', { name: /Restore/ })).toBeNull();
    });

    it('reads the time the sort reads and names a runner author (O05)', () => {
        card({ sort: 'created', entry: removed({ made_by: { kind: 'runner', runner_id: 'r', runner_name: 'seed-ideas' } }) });
        const el = document.querySelector('[data-rm-card="g1"]')!;
        expect(el).toHaveTextContent('Created 2 Oct · 09:12');
        expect(el).toHaveTextContent('seed-ideas');
    });

    it('under the Last removed sort says when it was removed (TG-04)', () => {
        card({ sort: 'removed' });
        expect(document.querySelector('[data-rm-card="g1"]')).toHaveTextContent('Removed 6 Oct · 10:00');
    });

    it('shows its tags as one read-only line of chips, and no row without tags (TG-04)', () => {
        card({ entry: removed({ tags: ['done', 'needs review'] }) });
        const row = document.querySelector<HTMLElement>('[data-rm-tags]')!;
        expect([...row.querySelectorAll('li')].map(li => li.textContent)).toEqual(['done', 'needs review']);
        expect(row.className).toContain('overflow-hidden');
        expect(row.className).toContain('whitespace-nowrap');
        expect(row.querySelector('button')).toBeNull();
        cleanup();
        card();
        expect(document.querySelector('[data-rm-tags]')).toBeNull();
    });

    it('lists two names and "+n more" past three files, and no preview without one (O05)', () => {
        card({ entry: removed({ files: ['a.md', 'b.md', 'c.md', 'd.md'], file_count: 250, preview: null }) });
        const el = document.querySelector('[data-rm-card="g1"]')!;
        expect(el).toHaveTextContent('Files (250)');
        expect(el).toHaveTextContent('+248 more');
        expect(el).not.toHaveTextContent('c.md');
        expect(el.querySelector('pre')).toBeNull();
    });

    it('the selected card has a solid edge and Restore, which does not select (O05)', () => {
        const props = card({ selected: true });
        const el = document.querySelector('[data-rm-card="g1"]')!;
        expect(el.className).toContain('border-solid');
        fireEvent.click(screen.getByRole('button', { name: /Restore/ }));
        expect(props.onRestore).toHaveBeenCalledTimes(1);
        expect(props.onClick).not.toHaveBeenCalled();
        fireEvent.click(el);
        expect(props.onClick).toHaveBeenCalledTimes(1);
    });

    it('the journal\'s shelf card has a solid teal edge, over the selected card\'s ink-3 (journal teal)', () => {
        history.fetchCellHistory.mockReturnValue(new Promise(() => {})); // the journal body's versions, never answered
        const edge = () => document.querySelector('[data-rm-card="g1"]')!.className.split(/\s+/);
        card({ journalStyle: true });
        expect(edge()).toEqual(expect.arrayContaining(['border', 'rounded-lg', 'border-solid', 'border-teal', 'transition-[border-color]']));
        expect(edge()).not.toContain('border-dashed');
        cleanup();
        card({ journalStyle: true, selected: true });
        expect(edge()).toContain('border-teal');
        expect(edge()).not.toContain('border-ink-3');
        cleanup();
        card({ selected: true });
        expect(edge()).toContain('border-ink-3');
        expect(edge()).not.toContain('border-teal');
    });

    it('says what a second click does while it is the picked card (O05)', () => {
        card({ followHint: true });
        expect(document.querySelector('[data-follow-hint]')).toHaveTextContent('Click again to follow its lines');
    });

    it('in the journal style lists its versions, its files and its runs (O05)', async () => {
        history.fetchCellHistory.mockResolvedValue({
            nodes: [
                { node_id: 'n2', kind: 'run', run: { run_id: 'r1', status: 'completed', phase: null, created_at: null, prompt: null, generator_name: 'challenge' },
                    versions: [{ cell_id: 'g1-v2', index: 1, created_at: at(4, 12, 15), name: null, file_count: 3, generator_name: 'challenge' }], config: [], logs: [] },
                { node_id: 'n1', kind: 'files', run: null,
                    versions: [{ cell_id: 'g1-v1', index: 0, created_at: at(2, 9, 12), name: null, file_count: 1 }], config: [], logs: [] },
            ],
            total_versions: 2,
            current: { node_id: 'n2', version_cell_id: 'g1-v2' },
        });
        const run: JournalEvent = {
            t: at(4, 12, 15), kind: 'run', by: { kind: 'runner', runner_id: 'r', runner_name: 'challenge' },
            cells: [{ cell_id: 'g1', name: 'Merge on conflict', status: 'removed' }], files: [], files_total: 0, version_id: 'g1-v2',
            run: { run_id: 'r1', runner_id: 'r', runner_name: 'challenge', status: 'completed', outcome: 'pass', note: 'reproduced the LWW loss',
                started_at: at(4, 12, 0), ended_at: at(4, 12, 15), files_written: [] },
        };
        useCollimateStore.setState(prev => ({
            overviewData: { ...prev.overviewData, journals: { g1: { events: [run], loading: false, complete: true, error: null } } },
        }));
        card({ journalStyle: true, entry: removed({ files: ['a.md', 'b.md', 'c.md', 'd.md'], file_count: 4 }) });
        const el = document.querySelector<HTMLElement>('[data-rm-card="g1"]')!;
        expect(el.style.minHeight).toBe('var(--rm-ch, 273px)');
        expect(el).toHaveTextContent('d.md');
        expect(el).not.toHaveTextContent('more');
        await waitFor(() => expect(el).toHaveTextContent('Versions (2)'));
        expect(history.fetchCellHistory).toHaveBeenCalledWith('chan', 'g1', expect.any(AbortSignal));
        expect(el.querySelector('[data-card-versions]')!.children).toHaveLength(2);
        expect(el.querySelector('[data-card-runs]')).toHaveTextContent('challengereproduced the LWW loss4 Oct 12:15');
        expect(el.querySelector('[data-card-run="pass"]')).not.toBeNull();
    });
});
