import { fileFixture } from '@/test/fileFixtures';
// Tree rows of a live run's files: every file under a live run's
// `_runs/<label>/` folder is that run's lock. Its row wears a lock glyph
// naming the run and offers no delete, rename or pin (hover actions, F2,
// Delete) — the file stays openable and draggable (a drag copies). A
// folder holding a locked file is locked the same way, and a folder under
// `_runs/` takes no new file.

import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';

import { FileTree } from './FileTreeNode';
import { nodeLockOwner } from '@/lib/fileLocks';
import { buildFileTree } from '@/lib/fileTree';
import type { RunRecord, VirtualFile } from '@/lib/types';

const mk = (name: string): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp: '2024-01-01T00:00:00Z'}));
const RUN: RunRecord = {
    runId: 'run-1', cellId: 'cell-1', channel: 'ch', runnerId: 'r', runnerName: 'Runner R',
    generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111',
};
const LOCKED = `${RUN.folder}/chat/conversation.chat`;
const lockOwnerOf = (path: string) => (path.startsWith(`${RUN.folder}/`) ? RUN : null);

function renderTree() {
    const onRename = vi.fn();
    const onDelete = vi.fn();
    const onTogglePin = vi.fn();
    const onCreateFile = vi.fn();
    const onOpenFile = vi.fn();
    render(
        <FileTree
            nodes={buildFileTree([mk('notes.md'), mk(LOCKED)])}
            level={0}
            onOpenFile={onOpenFile}
            onCreateFile={onCreateFile}
            onRename={onRename}
            onDelete={onDelete}
            onTogglePin={onTogglePin}
            readOnly={false}
            lockOwnerOf={lockOwnerOf}
            onDropFiles={vi.fn()}
            transfer={{
                onContextMenu: vi.fn(), onDragStartNode: vi.fn(), onDragEndNode: vi.fn(),
                isInternalDrag: () => false, dropEffectFor: () => 'copy', onDropInto: () => false,
            }}
        />,
    );
    return { onRename, onDelete, onTogglePin, onCreateFile, onOpenFile };
}

const row = (path: string) => document.querySelector(`[data-filenode-path="${path}"]`) as HTMLElement;

describe('file tree rows — run locks', () => {
    it('a locked file wears a lock naming its run and offers no delete, rename or pin', () => {
        renderTree();
        const locked = row(LOCKED);
        const lock = locked.querySelector('[data-testid="file-lock"]') as HTMLElement;
        expect(lock.getAttribute('title')).toContain('Runner R 11111111');
        expect(locked.querySelector('[title="Delete"]')).toBeNull();
        expect(locked.querySelector('[title="Rename"]')).toBeNull();
        expect(locked.querySelector('[title="Pin as preview"]')).toBeNull();
        // An ordinary file keeps all three.
        const plain = row('notes.md');
        expect(plain.querySelector('[data-testid="file-lock"]')).toBeNull();
        expect(plain.querySelector('[title="Delete"]')).not.toBeNull();
        expect(plain.querySelector('[title="Rename"]')).not.toBeNull();
    });

    it('F2 and Delete do nothing on a locked row; Enter still opens it', () => {
        const { onDelete, onOpenFile } = renderTree();
        fireEvent.keyDown(row(LOCKED), { key: 'Delete' });
        fireEvent.keyDown(row(LOCKED), { key: 'F2' });
        expect(onDelete).not.toHaveBeenCalled();
        expect(row(LOCKED).querySelector('input')).toBeNull();
        fireEvent.keyDown(row(LOCKED), { key: 'Enter' });
        expect(onOpenFile).toHaveBeenCalledWith(LOCKED);
    });

    it('a locked row stays draggable (the drag copies)', () => {
        renderTree();
        expect(row(LOCKED).getAttribute('draggable')).toBe('true');
    });

    it('a folder holding a locked file is locked too, and a `_runs/` folder takes no new file', () => {
        const { onDelete } = renderTree();
        for (const folder of ['_runs', RUN.folder, `${RUN.folder}/chat`]) {
            const r = row(folder);
            expect(r.querySelector('[data-testid="file-lock"]')).not.toBeNull();
            expect(r.querySelector('[title="Delete"]')).toBeNull();
            expect(r.querySelector('[title="Rename"]')).toBeNull();
            expect(r.querySelector('[title="New file in this folder"]')).toBeNull();
            fireEvent.keyDown(r, { key: 'Delete' });
        }
        expect(onDelete).not.toHaveBeenCalled();
    });

    it('a `_runs/` folder row refuses a drop instead of passing it to the tree root', () => {
        const rootDragOver = vi.fn();
        const rootDrop = vi.fn();
        const onDropFiles = vi.fn();
        const onDropInto = vi.fn(() => false);
        render(
            <div onDragOver={rootDragOver} onDrop={rootDrop}>
                <FileTree
                    nodes={buildFileTree([mk('src/a.md'), mk(LOCKED)])}
                    level={0}
                    onOpenFile={vi.fn()}
                    onCreateFile={vi.fn()}
                    onRename={vi.fn()}
                    onDelete={vi.fn()}
                    readOnly={false}
                    lockOwnerOf={lockOwnerOf}
                    onDropFiles={onDropFiles}
                    transfer={{
                        onContextMenu: vi.fn(), onDragStartNode: vi.fn(), onDragEndNode: vi.fn(),
                        isInternalDrag: () => false, dropEffectFor: () => 'copy', onDropInto,
                    }}
                />
            </div>,
        );
        const dataTransfer = { types: ['Files'], files: [], items: [], dropEffect: 'none' };
        // The dragover is swallowed and not accepted: no drop target lights up.
        expect(fireEvent.dragOver(row('_runs'), { dataTransfer })).toBe(true);
        expect(rootDragOver).not.toHaveBeenCalled();
        // A drop that still arrives is consumed: default prevented, never bubbled.
        expect(fireEvent.drop(row('_runs'), { dataTransfer })).toBe(false);
        expect(rootDrop).not.toHaveBeenCalled();
        expect(onDropFiles).not.toHaveBeenCalled();
        expect(onDropInto).not.toHaveBeenCalled();
    });

    it('nodeLockOwner reads a file\'s owner or the first owner under a folder', () => {
        const tree = buildFileTree([mk('notes.md'), mk(LOCKED)]);
        expect(nodeLockOwner(tree['notes.md'], lockOwnerOf)).toBeNull();
        expect(nodeLockOwner(tree['_runs'], lockOwnerOf)).toBe(RUN);
        expect(nodeLockOwner(tree['_runs'], undefined)).toBeNull();
    });
});
