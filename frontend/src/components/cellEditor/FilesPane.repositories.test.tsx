import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import { FilesPane, type FilesPaneProps } from './FilesPane';
import { createDefaultLayout, setFilesPane } from '@/lib/cellLayout';
import type { Cell, CellRepository } from '@/lib/types';

const repo = (path: string, patch: Partial<CellRepository> = {}): CellRepository =>
    ({ path, branch: 'main', upstream: null, ahead: null, behind: null, detached: false, ...patch });
const layout = (viewMode: 'tree' | 'folder') => setFilesPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }), { collapsed: false, viewMode });
const renderPane = (cell: Cell, viewMode: 'tree' | 'folder' = 'tree') => {
    const props: FilesPaneProps = { cell, channel: 'ch', layout: layout(viewMode), onLayoutChange: vi.fn(), onOpenFile: vi.fn(), onUpdateFiles: vi.fn(), readOnly: false, activePath: null };
    return render(<FilesPane {...props} />);
};
const row = (path: string) => document.querySelector(`[data-filenode-path="${path}"]`) as HTMLElement;

// The Files pane hands the snapshot's repositories to its views: a folder
// repository's chip on its row, a row for one with no file, and a note when
// the snapshot lists only the first of them (§11).
describe('FilesPane: repositories', () => {
    const cell: Cell = {
        id: 'c1', name: 'Repo', files: [fileFixture('app/a.txt', 'a'), fileFixture('top.md', 't')],
        repositories: [repo(''), repo('app', { branch: 'dev', ahead: 3 }), repo('vendor')],
    };

    it('the tree puts each folder repository’s chip on its row and a read-only row for one with no file; the root’s is the head’s', () => {
        renderPane(cell);
        expect(within(row('app')).getByTestId('repo-chip')).toHaveTextContent('dev↑3');
        expect(within(row('vendor')).getByTestId('repo-chip')).toHaveTextContent('main');
        expect(row('vendor').getAttribute('draggable')).toBe('false');
        expect(screen.getAllByTestId('repo-chip')).toHaveLength(2);
        expect(screen.queryByTestId('repo-overflow')).toBeNull();
    });

    it('the folder view shows the chips on its folder tiles', () => {
        renderPane(cell, 'folder');
        expect(screen.getAllByTestId('repo-chip').map(chip => chip.textContent)).toEqual(['dev↑3', 'main']);
    });

    it('a cell with more repositories than the snapshot lists says how many are not shown', () => {
        renderPane({ ...cell, repositories_overflow: 4 });
        expect(screen.getByTestId('repo-overflow')).toHaveTextContent('4 more repositories are not shown');
        renderPane({ ...cell, id: 'c2', repositories_overflow: 1 });
        expect(screen.getAllByTestId('repo-overflow')[1]).toHaveTextContent('1 more repository is not shown');
    });
});
