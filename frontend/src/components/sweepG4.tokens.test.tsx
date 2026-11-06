// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
// Sweep G4 (files and folders) paints only on the skin tokens.
// A source scan fails the moment an old palette name, a colour literal, a
// Tailwind palette utility or a shadcn theme utility comes back into one of
// these files; the rendered checks pin each surface's role: brass marks the
// selection, teal a live or on state (drop target, pin, open file), red the
// destructive and failed, green an addition, ink tiers everything else.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/components/OpenWithSubmenu', () => ({ OpenWithSubmenu: () => null, hasOpenWithChoices: () => false }));
vi.mock('@/components/CompareWithSubmenu', () => ({ CompareWithSubmenu: () => null }));

import { FileTree } from './FileTreeNode';
import { FolderTile } from './FolderTile';
import { FileContextMenu } from './FileContextMenu';
import { AddFileDialog } from './AddFileDialog';
import { getFileIcon } from './fileIcon';
import { btn, btnPrimary, dialog } from '@/components/ui/formStyles';
import { menuItem, menuItemDanger, menuSurface } from '@/components/ui/menuStyles';
import { buildFileTree } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

const FILES = [
    'FileTreeNode.tsx',
    'FolderView.tsx',
    'FileBrowser.tsx',
    'fileIcon.tsx',
    'FolderTile.tsx',
    'FileContextMenu.tsx',
    'AddFileDialog.tsx',
    'InCellDropFrame.tsx',
];

const OLD = RETIRED_TOKENS;
const PALETTE = 'slate|zinc|gray|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose';
const FORBIDDEN: Array<[string, RegExp]> = [
    ['old token name', OLD],
    ['hex colour', /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{1}|[0-9a-fA-F]{3}|[0-9a-fA-F]{5})?\b/],
    ['functional colour', /\b(?:rgba?|hsla?|oklch)\(/],
    ['palette utility', new RegExp(`\\b[a-z-]*-(?:${PALETTE})-\\d{2,3}\\b`)],
    ['white/black utility', /\b(?:bg|text|border|ring|fill|stroke)-(?:white|black)\b/],
    ['shadcn theme utility', /\b(?:bg|text|border|ring|placeholder:text)-(?:primary|secondary|destructive|background|foreground|accent|muted|input|ring|popover|card)(?:-foreground)?\b/],
    ['stock shadow', /\bshadow-(?:xs|sm|md|lg|xl|2xl|inner)\b/],
];

afterEach(cleanup);

describe('Sweep G4 token sweep', () => {
    it.each(FILES)('%s carries no old token, colour literal or palette utility (S04)', (file) => {
        const src = readFileSync(join(__dirname, file), 'utf8');
        for (const [what, re] of FORBIDDEN) {
            const hit = src.match(re);
            expect(hit?.[0], `${file}: ${what}`).toBeUndefined();
        }
    });
});

/** An element's whole class tokens, so `text-ink` cannot match `text-ink-2` or `hover:text-ink`. */
const tokens = (el: Element) => (el.getAttribute('class') ?? '').split(/\s+/).filter(Boolean);

/** Every class in the subtree, for asserting no old name survives a render. */
const allClasses = (root: Element) => [root, ...root.querySelectorAll('*')].map(el => el.getAttribute('class') ?? '').join(' ');

const mk = (name: string): VirtualFile => (fileFixture(name, '', {language: 'markdown', timestamp: 't'}));
const row = (path: string) => document.querySelector(`[data-filenode-path="${path}"]`) as HTMLElement;

describe('Sweep G4 surfaces', () => {
    it('the open file row takes the raised fill; pin is teal; diff letters are green and red (S04)', () => {
        const { container } = render(
            <FileTree
                nodes={buildFileTree([mk('open.md'), mk('other.md'), mk('docs/new.md')])}
                level={0} readOnly={false} activePath="open.md" pinnedName="other.md"
                onOpenFile={vi.fn()} onCreateFile={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()}
                onTogglePin={vi.fn()} onDropFiles={vi.fn()}
                diffTargetFiles={{ 'other.md': 'x' }}
            />,
        );
        // The open row is sheet-2 in ink; the rest read ink-2 (C12).
        const openRow = tokens(row('open.md'));
        expect(openRow).toEqual(expect.arrayContaining(['bg-sheet-2', 'text-ink']));
        expect(openRow).not.toContain('text-ink-2');
        const otherRow = tokens(row('other.md'));
        expect(otherRow).toContain('text-ink-2');
        expect(otherRow).not.toContain('bg-sheet-2');
        expect(otherRow).not.toContain('text-ink');
        expect(tokens(screen.getByLabelText('Unpin'))).toContain('text-teal');
        expect(screen.getAllByTitle('Added')[0].className).toContain('text-green');
        const del = row('other.md').querySelector('[title="Delete"]')!;
        expect(del.className).toContain('hover:text-red');
        expect(allClasses(container)).not.toMatch(OLD);
    });

    it('a selected folder tile is brass, an unselected one hovers on sheet-2 (S04)', () => {
        const props = { isFolder: true, onSelect: vi.fn(), onOpen: vi.fn() };
        const { container } = render(<>
            <FolderTile identity="a" name="a" selected {...props} />
            <FolderTile identity="b" name="b" selected={false} {...props} />
        </>);
        expect(screen.getByTitle('a').className).toContain('border-brass');
        expect(screen.getByTitle('a').className).toContain('bg-brass-soft');
        expect(screen.getByTitle('b').className).toContain('hover:bg-sheet-2');
        expect(allClasses(container)).not.toMatch(OLD);
    });

    it('file icons are ink-3 glyphs (S04)', () => {
        const { container } = render(<>{getFileIcon('typescript', 'a.ts')}{getFileIcon('', 'b.excalidraw')}{getFileIcon('', 'c.md')}</>);
        for (const svg of container.querySelectorAll('svg')) expect(svg.getAttribute('class')).toContain('text-ink-3');
    });

    it('the file context menu renders on the menu vocabulary, Delete as the danger row (S04)', () => {
        render(
            <FileContextMenu
                x={0} y={0} target={{ kind: 'file', node: { name: 'n.md', path: 'n.md', type: 'file', children: {}, fileData: mk('n.md') } }}
                readOnly={false} clipboardLabel={null}
                onClose={vi.fn()} onOpen={vi.fn()} onNewFile={vi.fn()} onNewFolder={vi.fn()}
                onRename={vi.fn()} onDelete={vi.fn()}
            />,
        );
        const surface = screen.getByText('Open').closest('button')!.parentElement!;
        expect(surface.className).toContain(menuSurface);
        expect(screen.getByText('Open').closest('button')!.className).toBe(menuItem);
        expect(screen.getByText('Delete').closest('button')!.className).toBe(menuItemDanger);
        expect(allClasses(surface)).not.toMatch(OLD);
    });

    it('the add dialog is a dialog plate with a quiet Cancel and the primary Create (S04)', () => {
        render(<AddFileDialog x={0} y={0} basePath="" existingNames={[]} mode="folder" onCancel={vi.fn()} onCreate={() => ({ ok: false, error: 'nope' })} />);
        const cancel = screen.getByText('Cancel');
        expect(cancel.className).toBe(btn);
        expect(screen.getByText('Create').className).toBe(btnPrimary);
        const plate = cancel.closest('.fixed')!;
        for (const cls of dialog.split(' ')) expect(plate.className).toContain(cls);
        fireEvent.click(screen.getByText('Create'));
        expect(screen.getByText('Enter a folder name').className).toContain('text-red');
        expect(allClasses(plate)).not.toMatch(OLD);
    });
});
