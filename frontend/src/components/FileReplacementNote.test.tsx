// @vitest-environment jsdom
// The file editor says when the revision it shows is a run's deliberate
// replacement of a newer save, and only then.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import type { HistoryResponse } from '@/lib/api/history';

const history = vi.hoisted(() => ({ fetchCellHistory: vi.fn() }));
vi.mock('@/lib/api/history', () => history);

import { FileReplacementNote } from './FileReplacementNote';
import { findFileReplacement, resetFileReplacements } from '@/lib/fileReplacement';

const response: HistoryResponse = {
    nodes: [{
        node_id: 'run:r1:v0', kind: 'run',
        run: { run_id: 'r1', status: 'completed', phase: null, created_at: null, prompt: null,
            runner_id: 'agent-x', generator_name: 'agent' },
        versions: [{ cell_id: 'v1', index: 1, created_at: null, name: 'Agent', file_count: 1, replaced: [
            { path: 'notes.md', revision: 3, writer_id: 'browser', writer: { kind: 'user' }, file_revision: 5 }] }],
        config: [], logs: [],
    }],
    total_versions: 1, current: { node_id: 'run:r1:v0', version_cell_id: 'v1' },
};

beforeEach(() => { resetFileReplacements(); history.fetchCellHistory.mockResolvedValue(response); });
afterEach(() => vi.clearAllMocks());

describe('FileReplacementNote', () => {
    it("says a run replaced the user's revision deliberately on the revision that write became", async () => {
        render(<FileReplacementNote channel="ch" cellId="cell" path="notes.md" revision={5} writerId="run:r1:w" />);
        await waitFor(() => expect(screen.getByTestId('file-replaced').textContent).toBe(
            'Run agent-x replaced your revision 3 deliberately. The replaced save is kept in History as the version before it.'));
        expect(history.fetchCellHistory).toHaveBeenCalledWith('ch', 'cell');
    });

    it('shows nothing for a later revision, another path, or a writer that is not a run', async () => {
        const { rerender } = render(<FileReplacementNote channel="ch" cellId="cell" path="notes.md" revision={6} writerId="run:r1:w" />);
        await waitFor(() => expect(history.fetchCellHistory).toHaveBeenCalledTimes(1));
        expect(screen.queryByTestId('file-replaced')).toBeNull();
        rerender(<FileReplacementNote channel="ch" cellId="cell" path="notes.md" revision={5} writerId="browser" />);
        expect(screen.queryByTestId('file-replaced')).toBeNull();
        expect(history.fetchCellHistory).toHaveBeenCalledTimes(1);
        expect(findFileReplacement(response, 'other.md', 5)).toBeNull();
    });
});
