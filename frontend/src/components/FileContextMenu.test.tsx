import { fileFixture } from '@/test/fileFixtures';
// The file context menu on a live run's files: a locked file (or a folder
// holding one) can be copied and duplicated — in the cell, outside
// `_runs/` — but never cut, renamed, deleted or pinned; and nothing is
// created or pasted into a folder under `_runs/`.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/components/OpenWithSubmenu', () => ({ OpenWithSubmenu: () => null, hasOpenWithChoices: () => false }));
vi.mock('@/components/CompareWithSubmenu', () => ({ CompareWithSubmenu: () => null }));

import { FileContextMenu, type FileMenuTarget } from './FileContextMenu';
import type { TreeNode } from '@/lib/fileTree';

const node = (path: string, type: 'file' | 'folder'): TreeNode => ({
    name: path.split('/').pop()!, path, type, children: {},
    ...(type === 'file' ? { fileData: fileFixture(path, '', {language: 'plaintext', timestamp: 't'}) } : {}),
});

const open = (target: FileMenuTarget, locked: boolean, clipboardLabel: string | null = "'notes.md'") => render(
    <FileContextMenu
        x={0} y={0} target={target} readOnly={false} locked={locked}
        clipboardLabel={clipboardLabel} canPin
        onClose={vi.fn()} onOpen={vi.fn()}
        onNewFile={vi.fn()} onNewFolder={vi.fn()}
        onRename={vi.fn()} onDelete={vi.fn()} onCut={vi.fn()} onCopy={vi.fn()}
        onPaste={vi.fn()} onDuplicate={vi.fn()} onTogglePin={vi.fn()}
    />,
);
const items = () => screen.getAllByRole('button').map(b => b.textContent);

afterEach(cleanup);

describe('FileContextMenu — run locks', () => {
    it('a locked file: Open, Copy and Duplicate only', () => {
        open({ kind: 'file', node: node('_runs/r-11111111/chat/c.chat', 'file') }, true);
        expect(items()).toEqual(['Open', 'Copy', 'Duplicate']);
    });

    it('an ordinary file keeps every item', () => {
        open({ kind: 'file', node: node('notes.md', 'file') }, false);
        expect(items()).toEqual(['Open', 'Copy', 'Cut', 'Duplicate', 'Pin as preview', 'Rename', 'Delete']);
    });

    it('a folder under `_runs/` holding locked files: no create, cut, paste, rename or delete', () => {
        open({ kind: 'folder', node: node('_runs/r-11111111', 'folder') }, true);
        expect(items()).toEqual(['Open', 'Copy']);
    });

    it('the empty area of a `_runs/` folder offers nothing to create or paste', () => {
        const { container } = open({ kind: 'empty', basePath: '_runs/r-11111111' }, false);
        expect(container.textContent).toBe('');
    });
});

describe('FileContextMenu — Paste (FP-05)', () => {
    it('a writable folder and the empty area offer Paste here with no internal clipboard (FP-05)', () => {
        open({ kind: 'folder', node: node('docs', 'folder') }, false, null);
        expect(items()).toContain('Paste here');
        cleanup();
        open({ kind: 'empty', basePath: '' }, false, null);
        expect(items()).toEqual(['New file', 'New folder', 'Paste here']);
    });

    it('the internal clipboard names the item (FP-05)', () => {
        open({ kind: 'empty', basePath: 'docs' }, false, "'components'");
        expect(items()).toContain("Paste 'components'");
        expect(items()).not.toContain('Paste here');
    });

    it('nothing is pasted under `_runs/`, a read-only cell or a file row (FP-05)', () => {
        open({ kind: 'folder', node: node('_runs/r-1/out', 'folder') }, false, null);
        expect(items().some(label => label?.startsWith('Paste'))).toBe(false);
        cleanup();
        render(<FileContextMenu x={0} y={0} target={{ kind: 'empty', basePath: '' }} readOnly clipboardLabel={null}
            onClose={vi.fn()} onNewFile={vi.fn()} onNewFolder={vi.fn()} onPaste={vi.fn()} />);
        expect(screen.queryByText('Paste here')).toBeNull();
        cleanup();
        open({ kind: 'file', node: node('notes.md', 'file') }, false, null);
        expect(items().some(label => label?.startsWith('Paste'))).toBe(false);
    });
});
