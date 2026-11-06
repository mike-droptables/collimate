// The history rail names each pip's item in its title: a version that
// replaced a newer save deliberately (a run's acknowledged second attempt)
// carries that in its label, and so does its run node.

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { HistoryRail } from './HistoryRail';
import type { HistoryNode } from '@/lib/api';

const node: HistoryNode = {
    node_id: 'run:r1:v0', kind: 'run',
    run: { run_id: 'r1', status: 'completed', phase: null, created_at: null, prompt: null,
        runner_id: 'agent-x', generator_name: 'agent' },
    versions: [
        { cell_id: 'v0', index: 0, created_at: null, name: 'Agent', file_count: 1 },
        { cell_id: 'v1', index: 1, created_at: null, name: 'Agent', file_count: 1, replaced: [
            { path: 'notes.md', revision: 3, writer_id: 'browser', writer: { kind: 'user' }, file_revision: 4 }] },
    ],
    config: [], logs: [],
};

describe('HistoryRail', () => {
    it("labels a version that replaced the user's revision deliberately", () => {
        render(<HistoryRail nodes={[node]} selNodeId="run:r1:v0" selVersionId="v1" nested onSelect={vi.fn()} />);
        expect(screen.getByRole('slider').getAttribute('title'))
            .toBe('2 / 3 — Agent · Replaced your revision 3 of notes.md deliberately');
    });

    it('labels its run node too, and an unmarked version plainly', () => {
        const { unmount } = render(<HistoryRail nodes={[node]} selNodeId="run:r1:v0" selVersionId={null} nested={false} onSelect={vi.fn()} />);
        expect(screen.getByRole('slider').getAttribute('title'))
            .toBe('1 / 1 — Run · agent-x · completed · Replaced your revision 3 of notes.md deliberately');
        unmount();
        render(<HistoryRail nodes={[node]} selNodeId="run:r1:v0" selVersionId="v0" nested onSelect={vi.fn()} />);
        expect(screen.getByRole('slider').getAttribute('title')).toBe('3 / 3 — Agent');
    });
});
