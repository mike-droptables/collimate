import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, within } from '@testing-library/react';
import { fileFixture } from '@/test/fileFixtures';
import { FileTree } from './FileTreeNode';
import { buildFileTree } from '@/lib/fileTree';
import { repositoryLookup } from '@/lib/repositories';
import type { CellRepository } from '@/lib/types';

const repo = (path: string, patch: Partial<CellRepository> = {}): CellRepository =>
    ({ path, branch: 'main', upstream: 'origin/main', ahead: 0, behind: 2, detached: false, ...patch });
const repositories = [
    repo('app'), repo('empty', { branch: null, detached: true }),
    repo('lost', { error: { reason: 'the index is unreadable', rescued: true } }),
];

function renderTree(readOnly = false) {
    const onRename = vi.fn();
    const onDelete = vi.fn();
    const onCreateFile = vi.fn();
    const onDragStartNode = vi.fn();
    render(
        <FileTree
            nodes={buildFileTree([fileFixture('app/a.txt', 'a'), fileFixture('lost/b.txt', 'b'), fileFixture('notes.md', 'n')], ['app', 'empty', 'lost'])}
            level={0}
            onOpenFile={vi.fn()} onCreateFile={onCreateFile} onRename={onRename} onDelete={onDelete}
            readOnly={readOnly} repositoryAt={repositoryLookup(repositories)} cellId="c1" channel="ch"
            onDropFiles={vi.fn()}
            transfer={{
                onContextMenu: vi.fn(), onDragStartNode, onDragEndNode: vi.fn(),
                isInternalDrag: () => false, dropEffectFor: () => 'copy', onDropInto: () => false,
            }}
        />,
    );
    return { onRename, onDelete, onCreateFile, onDragStartNode };
}
const row = (path: string) => document.querySelector(`[data-filenode-path="${path}"]`) as HTMLElement;

// A folder repository's chip sits on its row (§11); a repository with no
// file in the cell still has a row, read-only but for its delete.
describe('file tree rows — repositories', () => {
    it('a repository folder wears its branch chip; other rows none', () => {
        renderTree();
        expect(within(row('app')).getByTestId('repo-chip')).toHaveTextContent('main↓2');
        expect(within(row('notes.md')).queryByTestId('repo-chip')).toBeNull();
    });

    it('a repository with no file gets a read-only row: a delete, no rename, new file, drag or menu', () => {
        const { onDelete, onRename } = renderTree();
        const empty = row('empty');
        expect(within(empty).getByTestId('repo-chip')).toHaveTextContent('(detached)');
        expect(empty.getAttribute('draggable')).toBe('false');
        expect(empty.querySelector('[title="Rename"]')).toBeNull();
        expect(empty.querySelector('[title="New file in this folder"]')).toBeNull();
        fireEvent.keyDown(empty, { key: 'F2' });
        expect(empty.querySelector('input')).toBeNull();
        fireEvent.click(empty.querySelector('[title="Delete"]') as HTMLElement);
        expect(onDelete).toHaveBeenCalledWith(expect.objectContaining({ path: 'empty', repositoryOnly: true }));
        expect(onRename).not.toHaveBeenCalled();
        // A real repository folder keeps every action.
        expect(row('app').querySelector('[title="Rename"]')).not.toBeNull();
        expect(row('app').getAttribute('draggable')).toBe('true');
    });

    it('keys on a rescue chip and in its menu stay there: no toggle, no folder delete', () => {
        const { onDelete } = renderTree();
        const shown = row('lost/b.txt') !== null;
        const chip = within(row('lost')).getByTestId('repo-chip');
        expect(fireEvent.keyDown(chip, { key: 'Backspace' })).toBe(true);
        fireEvent.click(chip);
        const retry = document.querySelector('[role="dialog"] button') as HTMLElement;
        expect(retry).toHaveTextContent('Retry');
        // Enter keeps its default: the button activates, the row stays shut.
        expect(fireEvent.keyDown(retry, { key: 'Enter' })).toBe(true);
        fireEvent.keyDown(retry, { key: 'Delete' });
        fireEvent.keyDown(retry, { key: 'Backspace' });
        expect(onDelete).not.toHaveBeenCalled();
        expect(row('lost/b.txt') !== null).toBe(shown);
    });
});
