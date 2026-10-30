// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chainVersions, hm, when } from './journalText';
import { fireEvent, render, waitFor } from '@testing-library/react';
import type { HistoryNode, HistoryResponse } from '@/lib/api/history';
import type { JournalEvent, JournalRun } from '@/lib/api/cells';

const history = vi.hoisted(() => ({ fetchCellHistory: vi.fn() }));
vi.mock('@/lib/api/history', async (original) => ({ ...await original<typeof import('@/lib/api/history')>(), ...history }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { JournalCardBody } from './JournalCardBody';

const NOW = new Date(2026, 9, 4, 15, 0);
const at = (h: number, m: number) => new Date(2026, 9, 4, h, m).toISOString();

const version = (id: string, h: number, m: number) => ({ cell_id: id, index: 0, created_at: at(h, m), name: null, file_count: 1 });
const node = (id: string, kind: 'run' | 'files', versions: ReturnType<typeof version>[], runner: string | null = null): HistoryNode => ({
    node_id: id, kind, versions, config: [], logs: [],
    run: kind === 'run' ? { run_id: id, runner_id: runner, status: 'completed', phase: null, created_at: null, prompt: null, generator_name: runner } : null,
});
// Newest node first; a node's versions oldest first.
const chain: HistoryResponse = {
    nodes: [
        node('n3', 'files', [version('v7', 14, 5)]),
        node('n2', 'run', [version('v4', 13, 0), version('v5', 13, 20), version('v6', 13, 40)], 'seed-ideas'),
        node('n1', 'files', [version('v1', 9, 12), version('v2', 9, 40), version('v3', 9, 58)]),
    ],
    total_versions: 7,
    current: { node_id: 'n3', version_cell_id: 'v7' },
};

const run = (id: string, over: Partial<JournalRun> = {}): JournalRun => ({
    run_id: id, runner_id: 'challenge', runner_name: 'challenge', status: 'completed', outcome: null, note: null,
    started_at: at(12, 0), ended_at: at(12, 15), files_written: [], ...over,
});
const runEvent = (t: string, r: JournalRun, cellId = 'a'): JournalEvent => ({
    t, kind: 'run', by: { kind: 'runner', runner_id: r.runner_id, runner_name: r.runner_name },
    cells: [{ cell_id: cellId, name: cellId, status: 'open' }], run: r, files: [], files_total: 0, version_id: null,
});

beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    history.fetchCellHistory.mockResolvedValue(chain);
    useCollimateStore.getState().dropOverviewData();
    useCollimateStore.setState(state => ({
        overviewData: {
            ...state.overviewData, channel: 'chan',
            journals: {
                a: {
                    loading: false, complete: true, error: null, events: [
                        runEvent(at(14, 0), run('r3', { note: 'reproduced the LWW loss' })),
                        runEvent(at(13, 0), run('r2', { status: 'failed', outcome: 'fail' })),
                        runEvent(at(10, 0), run('r1', { status: 'running', ended_at: null, started_at: at(14, 48) })),
                        // A run in a new cell made from another cell: not a run on this one.
                        { ...runEvent(at(9, 0), run('r0')), kind: 'run_cell', cells: [{ cell_id: 'z', name: 'Z', status: 'open' }, { cell_id: 'a', name: 'a', status: 'open' }] },
                    ],
                },
            },
        },
    }));
});
afterEach(() => vi.useRealTimers());

const files = Array.from({ length: 10 }, (_, i) => `docs/f${i}.md`);

describe('the journal-style card body', () => {
    it('lists the chain from the kept history route, newest first, with author and time, the current one marked (O09)', async () => {
        render(<JournalCardBody cellId="a" at={at(14, 5)} by="you" files={files} fileCount={12} />);
        expect(history.fetchCellHistory).toHaveBeenCalledWith('chan', 'a', expect.any(AbortSignal));
        await waitFor(() => expect(document.querySelector('[data-card-versions]')).not.toBeNull());
        const body = document.querySelector('[data-journal-card-body]')!;
        expect(body).toHaveTextContent('Versions (7)');
        const rows = [...document.querySelectorAll('[data-card-versions] li')].map(li => li.textContent);
        expect(rows).toEqual([
            'you4 Oct 14:05', 'seed-ideas4 Oct 13:40', 'seed-ideas4 Oct 13:20', 'seed-ideas4 Oct 13:00',
            'you4 Oct 09:58', 'you4 Oct 09:40', '+1 more',
        ]);
        expect(document.querySelectorAll('[data-pip="current"]')).toHaveLength(1);
        expect(document.querySelector('[data-card-versions] li [data-pip="current"]')!.className).toMatch(/bg-teal/);
    });

    it('a picked version row hands its rail node and version to the caller (O09)', async () => {
        const pick = vi.fn();
        render(<JournalCardBody cellId="a" at={null} by="you" files={[]} fileCount={0} onPickVersion={pick} />);
        await waitFor(() => expect(document.querySelector('[data-version-row="v5"]')).not.toBeNull());
        fireEvent.click(document.querySelector('[data-version-row="v5"]')!);
        expect(pick).toHaveBeenCalledWith('n2', 'v5');
    });

    it('shows the meta row, every file the payload carries with a count beyond them, and the runs on the cell (O06)', async () => {
        render(<JournalCardBody cellId="a" at={at(14, 5)} by="you" files={files} fileCount={12} />);
        const body = document.querySelector('[data-journal-card-body]')!;
        expect(body).toHaveTextContent('4 Oct · 14:05');
        expect(body).toHaveTextContent('Files (12)');
        for (const path of files) expect(body).toHaveTextContent(path.split('/').at(-1)!);
        expect(body).toHaveTextContent('+2 more');
        expect(body).toHaveTextContent('Runs (3)');
        const runs = [...document.querySelectorAll('[data-card-run]')];
        expect(runs.map(r => r.getAttribute('data-card-run'))).toEqual(['running', 'done', 'fail']);
        expect(runs.map(r => r.textContent)).toEqual([
            'challengerunning · 12mnow', 'challengereproduced the LWW loss4 Oct 14:00', 'challengefailed4 Oct 13:00',
        ]);
        await waitFor(() => expect(document.querySelector('[data-card-versions]')).not.toBeNull());
    });

    it('the meta row tells the current version\'s time and author once the chain is in (O06)', async () => {
        render(<JournalCardBody cellId="a" at={at(9, 12)} by="seed-ideas" files={[]} fileCount={0} />);
        const meta = () => document.querySelector('[data-journal-card-body] > div')!;
        expect(meta()).toHaveTextContent('4 Oct · 09:12');
        expect(meta()).toHaveTextContent('seed-ideas');
        await waitFor(() => expect(meta()).toHaveTextContent('4 Oct · 14:05'));
        expect(meta()).toHaveTextContent('you');
        expect(meta()).not.toHaveTextContent('seed-ideas');
    });

    it('lists every run on the cell, as the mock does (O06)', async () => {
        const many = Array.from({ length: 9 }, (_, i) => runEvent(at(11, i), run(`m${i}`)));
        useCollimateStore.setState(state => ({
            overviewData: { ...state.overviewData, journals: { a: { loading: false, complete: true, error: null, events: many } } },
        }));
        render(<JournalCardBody cellId="a" at={null} by="you" files={[]} fileCount={0} />);
        expect(document.querySelector('[data-journal-card-body]')).toHaveTextContent('Runs (9)');
        expect(document.querySelectorAll('[data-card-run]')).toHaveLength(9);
        expect(document.querySelector('[data-card-runs]')).not.toHaveTextContent('more');
        await waitFor(() => expect(history.fetchCellHistory).toHaveBeenCalled());
    });

    it('a single-version cell has no Versions list (O09)', async () => {
        history.fetchCellHistory.mockResolvedValue({ ...chain, nodes: [node('n1', 'files', [version('v1', 9, 0)])], total_versions: 1 });
        render(<JournalCardBody cellId="a" at={null} by="you" files={[]} fileCount={0} />);
        await waitFor(() => expect(history.fetchCellHistory).toHaveBeenCalled());
        expect(document.querySelector('[data-card-versions]')).toBeNull();
    });
});

describe("the chain's authors (O09)", () => {
    it('names a run node by its runner\'s name, else its id, else its generator (O09)', () => {
        const runNode = (runner_id: string | null, runner_name: string | null, generator_name: string | null): HistoryNode => ({
            ...node('n', 'run', [version('v', 9, 0)]),
            run: { run_id: 'n', runner_id, runner_name, status: 'completed', phase: null, created_at: null, prompt: null, generator_name },
        });
        const author = (n: HistoryNode) => chainVersions({ ...chain, nodes: [n] })[0].author;
        expect(author(runNode('challenge', 'Challenge it', 'gen'))).toBe('Challenge it');
        expect(author(runNode('challenge', null, 'gen'))).toBe('challenge');
        expect(author(runNode('challenge', '', 'gen'))).toBe('challenge');
        expect(author(runNode(null, null, 'gen'))).toBe('gen');
        expect(author(node('f', 'files', [version('v', 9, 0)]))).toBe('you');
    });
});

describe('the card\'s files (O01)', () => {
    it('tells two files that share a base name apart by their folders (O01)', async () => {
        render(<JournalCardBody cellId="a" at={null} by="you" files={['Scratch/scratch.txt', 'Scratch (Copy)/scratch.txt', 'Scratch/plot.py']} fileCount={3} />);
        const rows = [...document.querySelectorAll('[data-journal-card-body] ul:not([data-card-versions]):not([data-card-runs]) li')].map(li => li.textContent);
        expect(rows).toEqual(['Scratch/scratch.txt', 'Scratch (Copy)/scratch.txt', 'plot.py']);
        await waitFor(() => expect(history.fetchCellHistory).toHaveBeenCalled());
    });
});

describe("the kept history route's times (O09)", () => {
    it('reads the offset-less UTC text as UTC, the same instant as the ISO form (O09)', () => {
        expect(hm('2026-10-06 14:05:00')).toBe(hm('2026-10-06T14:05:00Z'));
        expect(when('2026-10-06 14:05:00')).toBe(when('2026-10-06T14:05:00Z'));
        expect(hm('2026-10-06T14:05:00+02:00')).toBe(hm('2026-10-06T12:05:00Z'));
    });
});
