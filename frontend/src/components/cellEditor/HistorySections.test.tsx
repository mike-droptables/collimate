import { fileFixture } from '@/test/fileFixtures';
// History sidebar interactions: the node header carries no exit ✕ (the
// Files pane's Tree / Folder tabs leave history) and, on the left dock,
// the content ends before the pane's collapse strip; a multi-version node advertises its drilldown with an
// "N versions" chip, and double-clicking the header toggles nested
// file-version scrubbing.

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { HistorySections, type HistorySectionsProps } from './HistorySections';
import type { HistoryNode } from '@/lib/api';
import { retiredOr } from '@/test/retiredTokens';

const version = (i: number) => ({
    cell_id: `v${i}`, index: i, created_at: null, name: `Notes v${i}`,
    file_count: 2,
});

const runNode = (nVersions: number): HistoryNode => ({
    node_id: 'run:1',
    kind: 'run',
    run: {
        run_id: 'r1', status: 'completed', phase: null, created_at: null,
        prompt: 'do the thing', runner_id: 'summarize',
        generator_name: 'agent',
    },
    versions: Array.from({ length: nVersions }, (_, i) => version(i)),
    config: [{ path: 'prompt.md', blob_hash: 'a'.repeat(64), size: 12 }],
    logs: [],
});

function renderSections(node: HistoryNode, overrides: Partial<HistorySectionsProps> = {}) {
    const props: HistorySectionsProps = {
        node,
        loading: false,
        error: null,
        files: [],
        baseFiles: {},
        nested: false,
        onToggleNested: vi.fn(),
        sectionsOpen: { config: true, files: true, logs: true },
        onToggleSection: vi.fn(),
        onOpenEntry: vi.fn(),
        activeEntry: null,
        ...overrides,
    };
    return { ...render(<HistorySections {...props} />), props };
}

describe('HistorySections', () => {
    it('the node header has no exit ✕; Branch is its last control', () => {
        renderSections(runNode(1), { onBranch: vi.fn() });
        expect(screen.queryByRole('button', { name: 'Exit version history' })).toBeNull();
        expect(document.querySelector('.lucide-x')).toBeNull();
        expect(screen.getByTitle('Branch a new cell from this version')).toBeTruthy();
    });

    it('a multi-version node shows the "N versions" chip; single-version does not', () => {
        const { unmount } = renderSections(runNode(3));
        expect(screen.getByText('3 versions')).toBeTruthy();
        unmount();

        renderSections(runNode(1));
        expect(screen.queryByText(/1 versions/)).toBeNull();
    });

    it('double-clicking the node header (or Files header) toggles the version drilldown', () => {
        const onToggleNested = vi.fn();
        renderSections(runNode(3), { onToggleNested });
        // Both the node header and the Files section header advertise the
        // gesture — either double-click drills down.
        const targets = screen.getAllByTitle('Double-click to scrub this node’s file versions');
        expect(targets.length).toBeGreaterThanOrEqual(1);
        fireEvent.dblClick(targets[0]);
        expect(onToggleNested).toHaveBeenCalledTimes(1);
    });

    it('the node light is a red ring for failed / timed_out / interrupted runs, not for stopped / completed (S04)', () => {
        for (const status of ['failed', 'timed_out', 'interrupted']) {
            const node = runNode(1);
            node.run!.status = status;
            const { unmount } = renderSections(node);
            expect(screen.getByTestId('history-node-light').className).toMatch(/\bborder-red\b/);
            unmount();
        }
        for (const status of ['stopped', 'completed']) {
            const node = runNode(1);
            node.run!.status = status;
            const { unmount } = renderSections(node);
            expect(screen.getByTestId('history-node-light').className).not.toMatch(/\bborder-red\b/);
            unmount();
        }
    });

    it('the node light speaks the run grammar on skin tokens: teal running, green pass, never the old palette (S04)', () => {
        const cases: Array<[string, RegExp]> = [
            ['running', /\bbg-teal\b/],
            ['completed', /\bbg-green\b/],
            ['stopped', /\bbg-ink-3\b/],
        ];
        for (const [status, want] of cases) {
            const node = runNode(1);
            node.run!.status = status;
            const { unmount } = renderSections(node);
            const light = screen.getByTestId('history-node-light');
            expect(light.className).toMatch(want);
            // The node header (light, label, versions toggle) is this file's own markup.
            expect(light.parentElement!.parentElement!.outerHTML).not.toMatch(retiredOr('plasma'));
            unmount();
        }
    });

    it('a single-version node ignores the double-click drilldown', () => {
        const onToggleNested = vi.fn();
        renderSections(runNode(1), { onToggleNested });
        expect(screen.queryAllByTitle('Double-click to scrub this node’s file versions')).toHaveLength(0);
        expect(onToggleNested).not.toHaveBeenCalled();
    });
});

it('offers copies only for config and log artifacts __F0773', () => {
    type CloneCategory = Parameters<NonNullable<HistorySectionsProps['onCloneToFiles']>>[0];
    // This compile-time contract must reject the removed historical clone.
    const acceptsHistoricalClone: 'file' extends CloneCategory ? true : false = false;
    expect(acceptsHistoricalClone).toBe(false);
    const node = runNode(1);
    node.logs = [{ path: 'logs.txt', blob_hash: 'b'.repeat(64), size: 10 }];
    const onCloneToFiles = vi.fn();
    renderSections(node, { onCloneToFiles, files: [fileFixture('past.txt', 'past', {language:'plaintext', timestamp:'t'})] });
    const buttons = screen.getAllByTitle("Copy into the cell's files (editable clone)");
    expect(buttons).toHaveLength(2);
    buttons.forEach(button => fireEvent.click(button));
    expect(onCloneToFiles.mock.calls).toEqual([['config', 'prompt.md'], ['log', 'logs.txt']]);
});
