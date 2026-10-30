import { fileFixture } from '@/test/fileFixtures';
// Tree rows in the Files pane (C12, the mock's `.tree .row`): 24px rows,
// a 6px gap, a 14px ink-3 glyph, the extension in mono ink-3, the pin in
// teal (brass in the selected cell), the open file on sheet-2 in ink; no
// open-tab dot and no chevron. The row actions (pin / rename / delete, a
// folder's + ) show on hover only and never shrink, so the NAME truncates
// to make room. jsdom evaluates no :hover, so these pin the classes that
// carry it.

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { FileTree } from './FileTreeNode';
import { ScrollArea } from '@/components/ui/scroll-area';
import { buildFileTree } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';

const mk = (name: string): VirtualFile => (fileFixture(name, '', {language: 'markdown', timestamp: '2024-01-01T00:00:00Z'}));

function renderTree(readOnly = false) {
    return render(
        <FileTree
            nodes={buildFileTree([mk('docs/notes.md'), mk('a-very-long-file-name-that-goes-on.md')])}
            level={0}
            onOpenFile={vi.fn()}
            activePath="a-very-long-file-name-that-goes-on.md"
            onCreateFile={vi.fn()}
            onRename={vi.fn()}
            onDelete={vi.fn()}
            onTogglePin={vi.fn()}
            readOnly={readOnly}
            onDropFiles={vi.fn()}
        />,
    );
}

const row = (path: string) => document.querySelector(`[data-filenode-path="${path}"]`) as HTMLElement;

describe('file tree rows', () => {
    it.each([
        ['typescript', 'drawing.excalidraw', 'pen-tool'],
        ['json', 'code.ts', 'file-code'],
        ['json', 'notes.md', 'braces'],
        ['plaintext', 'notes.md', 'file-text'],
        ['plaintext', 'data.bin', 'file'],
    ])('retains icon precedence for %s / %s (F0869)', (language, name, icon) => {
        render(<FileTree
            nodes={buildFileTree([{ ...mk(name), language }])}
            level={0} readOnly onOpenFile={vi.fn()} onCreateFile={vi.fn()}
            onRename={vi.fn()} onDelete={vi.fn()} onDropFiles={vi.fn()}
        />);
        expect(row(name).querySelector(`.lucide-${icon}`)).not.toBeNull();
    });

    it('forwards nested selection, pin and transfer callbacks unchanged (F0869)', () => {
        const path = 'docs/deep/notes.md';
        const onOpenFile = vi.fn(), onSelectionChange = vi.fn(), onTogglePin = vi.fn();
        const onContextMenu = vi.fn(), onDragStartNode = vi.fn();
        render(<FileTree
            nodes={buildFileTree([mk(path)])} level={0} readOnly={false}
            activePath={path} pinnedName={path}
            onOpenFile={onOpenFile} onSelectionChange={onSelectionChange}
            onTogglePin={onTogglePin} onCreateFile={vi.fn()} onRename={vi.fn()}
            onDelete={vi.fn()} onDropFiles={vi.fn()}
            transfer={{ onContextMenu, onDragStartNode, onDragEndNode: vi.fn(),
                isInternalDrag: () => false, dropEffectFor: () => 'copy', onDropInto: () => false }}
        />);
        const file = row(path);
        expect(file.getAttribute('aria-current')).toBe('true');
        fireEvent.focus(file);
        expect(onSelectionChange).toHaveBeenCalledExactlyOnceWith({ path, isFolder: false });
        fireEvent.keyDown(file, { key: 'Enter' });
        expect(onOpenFile).toHaveBeenCalledExactlyOnceWith(path);
        fireEvent.click(file.querySelector('[title="Unpin"]')!);
        expect(onTogglePin).toHaveBeenCalledExactlyOnceWith(path);
        fireEvent.contextMenu(file, { clientX: 3, clientY: 4 });
        expect(onContextMenu).toHaveBeenCalledWith(expect.objectContaining({ path }), 3, 4);
        fireEvent.dragStart(file);
        expect(onDragStartNode).toHaveBeenCalledWith(expect.objectContaining({ path }), expect.anything());
    });

    it('a right-click in a rename field keeps the browser menu: no row menu, nothing prevented', () => {
        const onContextMenu = vi.fn();
        render(<FileTree
            nodes={buildFileTree([mk('docs/notes.md')])} level={0} readOnly={false}
            onOpenFile={vi.fn()} onCreateFile={vi.fn()} onRename={vi.fn()}
            onDelete={vi.fn()} onDropFiles={vi.fn()}
            transfer={{ onContextMenu, onDragStartNode: vi.fn(), onDragEndNode: vi.fn(),
                isInternalDrag: () => false, dropEffectFor: () => 'copy', onDropInto: () => false }}
        />);
        // Both row kinds: the folder's handler and the file's.
        for (const path of ['docs', 'docs/notes.md']) {
            fireEvent.click(row(path).querySelector('[title="Rename"]')!);
            const field = row(path).querySelector('input')!;
            expect(fireEvent.contextMenu(field, { clientX: 3, clientY: 4 })).toBe(true);
        }
        expect(onContextMenu).not.toHaveBeenCalled();
        // The row around it still opens its own menu.
        expect(fireEvent.contextMenu(row('docs/notes.md'))).toBe(false);
        expect(onContextMenu).toHaveBeenCalledTimes(1);
    });

    it('show no open-tab dot, even on the active file', () => {
        renderTree();
        expect(screen.queryByLabelText('Open in a tab')).toBeNull();
        expect(row('a-very-long-file-name-that-goes-on.md').querySelector('.rounded-full')).toBeNull();
    });

    it('a file row: actions appear on hover and never shrink; the name truncates, the extension does not (C12)', () => {
        renderTree();
        const r = row('a-very-long-file-name-that-goes-on.md');
        const actions = (r.querySelector('[title="Rename"]') as HTMLElement).parentElement as HTMLElement;
        expect(actions.querySelector('[title="Pin as preview"]')).not.toBeNull();
        expect(actions.querySelector('[title="Delete"]')).not.toBeNull();
        expect(actions.className).toContain('hidden');
        expect(actions.className).toContain('group-hover/file-row:flex');
        expect(actions.className).toContain('flex-none');
        const name = screen.getByText('a-very-long-file-name-that-goes-on');
        expect(name.className).toContain('truncate');
        expect(name.className).toContain('min-w-0');
        // The extension never truncates with it.
        expect(name.nextElementSibling!.textContent).toBe('.md');
        expect(name.nextElementSibling!.className).toContain('flex-none');
    });

    it('a folder row: rename / delete and + appear on hover and never shrink', () => {
        renderTree();
        const r = row('docs');
        const actions = (r.querySelector('[title="Rename"]') as HTMLElement).parentElement as HTMLElement;
        expect(actions.className).toContain('hidden');
        expect(actions.className).toContain('flex-none');
        const plus = (r.querySelector('[title="New file in this folder"]') as HTMLElement).parentElement as HTMLElement;
        expect(plus.className).toContain('hidden');
        expect(plus.className).toContain('group-hover/file-row:flex');
        expect(screen.getByText('docs').className).toContain('min-w-0');
    });

    it('a folder row is the click target and hand-shaped edge to edge, like a file row', () => {
        renderTree();
        const folder = row('docs');
        expect(folder.className).toContain('cursor-pointer');
        expect(row('docs/notes.md')).not.toBeNull();
        // A click on the row itself (its padding, not an inner box) toggles.
        fireEvent.click(folder);
        expect(row('docs/notes.md')).toBeNull();
        fireEvent.click(folder);
        expect(row('docs/notes.md')).not.toBeNull();
        // The row actions don't toggle it.
        fireEvent.click(folder.querySelector('[title="Rename"]')!);
        expect(row('docs/notes.md')).not.toBeNull();
    });

    it('hover never animates colour: only the press scale transitions', () => {
        renderTree();
        for (const p of ['docs', 'docs/notes.md']) {
            const cls = row(p).className;
            expect(cls).toContain('transition-[scale]');
            expect(cls).not.toMatch(/transition-(colors|\[background)/);
        }
    });

    it('rows are 24px of 12.5px type with a 6px gap, indented 12px a level from 6px; a 14px ink-3 glyph (C12)', () => {
        renderTree();
        for (const [p, indent] of [['docs', '6px'], ['docs/notes.md', '18px']] as const) {
            const r = row(p);
            expect(r.className).toMatch(/\bh-6\b/);
            expect(r.className).toMatch(/text-\[0\.78125rem\]/);
            expect(r.className).toMatch(/\bgap-1\.5\b/);
            expect(r.style.paddingLeft).toBe(indent);
            const glyph = r.querySelector('svg')!.parentElement!;
            expect(glyph.className).toContain('text-ink-3');
            expect(glyph.className).toContain('[&>svg]:w-3.5');
        }
    });

    it('a folder shows its open or closed glyph and no chevron (C12)', () => {
        renderTree();
        const folder = row('docs');
        expect(folder.querySelector('.lucide-folder-open')).not.toBeNull();
        expect(folder.querySelector('[class*="chevron"]')).toBeNull();
        fireEvent.click(folder);
        expect(folder.querySelector('.lucide-folder')).not.toBeNull();
        expect(folder.querySelector('.lucide-folder-open')).toBeNull();
    });

    it('the extension is mono 11px ink-3 (ink-2 on the open file); the open row is sheet-2 in ink (C12)', () => {
        renderTree();
        const ext = (p: string) => Array.from(row(p).querySelectorAll('span')).find(s => s.textContent === '.md')!;
        expect(ext('docs/notes.md').className).toMatch(/\bfont-mono\b/);
        expect(ext('docs/notes.md').className).toMatch(/text-\[0\.6875rem\]/);
        expect(ext('docs/notes.md').className).toMatch(/\btext-ink-3\b/);
        expect(ext('a-very-long-file-name-that-goes-on.md').className).toMatch(/\btext-ink-2\b/);
        const open = row('a-very-long-file-name-that-goes-on.md');
        expect(open.className).toMatch(/\bbg-sheet-2\b/);
        expect(open.className).toMatch(/\btext-ink\b/);
        expect(row('docs/notes.md').className).toMatch(/\btext-ink-2\b/);
        expect(row('docs/notes.md').className).toMatch(/\bhover:bg-sheet-2\b/);
    });

    it('a dotfile is all extension (C12)', () => {
        render(<FileTree nodes={buildFileTree([mk('.options_stack')])} level={0} readOnly
            onOpenFile={vi.fn()} onCreateFile={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onDropFiles={vi.fn()} />);
        const ext = screen.getByText('.options_stack');
        expect(ext.className).toMatch(/\bfont-mono\b/);
    });

    it('the pin sits at the row\'s end in teal, brass in the selected cell (C12)', () => {
        const tree = (selected: boolean) => (
            <CellSelectionContext.Provider value={selected}>
                <FileTree nodes={buildFileTree([mk('a.md')])} level={0} readOnly pinnedName="a.md"
                    onOpenFile={vi.fn()} onCreateFile={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onDropFiles={vi.fn()} />
            </CellSelectionContext.Provider>
        );
        const { rerender } = render(tree(false));
        const pin = () => screen.getByLabelText('Pinned as preview');
        expect(pin().getAttribute('class')).toMatch(/\bml-auto\b/);
        expect(pin().getAttribute('class')).toMatch(/\btext-teal\b/);
        rerender(tree(true));
        expect(pin().getAttribute('class')).toMatch(/\btext-brass\b/);
        expect(pin().getAttribute('class')).not.toMatch(/teal/);
    });

    it('one pin control: the pinned file\'s pin unpins it, an unpinned file\'s hover pin pins it', () => {
        const onTogglePin = vi.fn();
        render(
            <FileTree
                nodes={buildFileTree([mk('a.md'), mk('b.md')])}
                level={0}
                onOpenFile={vi.fn()}
                onCreateFile={vi.fn()}
                onRename={vi.fn()}
                onDelete={vi.fn()}
                pinnedName="a.md"
                onTogglePin={onTogglePin}
                readOnly={false}
                onDropFiles={vi.fn()}
            />,
        );
        const pinned = row('a.md'), other = row('b.md');
        // The pinned file: its filled pin is the button — no separate unpin icon.
        expect(pinned.querySelector('.lucide-pin-off')).toBeNull();
        expect(pinned.querySelector('[title="Pin as preview"]')).toBeNull();
        const unpin = pinned.querySelector('[title="Unpin"]') as HTMLElement;
        expect(unpin.querySelector('.lucide-pin')).not.toBeNull();
        // Always shown, not a hover action.
        expect(unpin.closest('.hidden')).toBeNull();
        fireEvent.click(unpin);
        expect(onTogglePin).toHaveBeenLastCalledWith('a.md');
        // The other file pins from its hover actions (replacing the pin).
        expect(other.querySelector('[title="Unpin"]')).toBeNull();
        fireEvent.click(other.querySelector('[title="Pin as preview"]') as HTMLElement);
        expect(onTogglePin).toHaveBeenLastCalledWith('b.md');
        expect(onTogglePin).toHaveBeenCalledTimes(2);
    });

    it('read-only rows carry no actions', () => {
        renderTree(true);
        expect(document.querySelector('[title="Rename"]')).toBeNull();
        expect(document.querySelector('[title="Delete"]')).toBeNull();
    });

    it('read-only rows still open / toggle on Enter, but F2 and Delete do nothing', () => {
        const onOpenFile = vi.fn(), onDelete = vi.fn();
        render(
            <FileTree
                nodes={buildFileTree([mk('docs/notes.md'), mk('a.md')])}
                level={0}
                onOpenFile={onOpenFile}
                onCreateFile={vi.fn()}
                onRename={vi.fn()}
                onDelete={onDelete}
                readOnly
                onDropFiles={vi.fn()}
            />,
        );
        fireEvent.keyDown(row('a.md'), { key: 'Enter' });
        expect(onOpenFile).toHaveBeenCalledWith('a.md');
        fireEvent.keyDown(row('docs'), { key: 'Enter' });
        expect(row('docs/notes.md')).toBeNull();
        for (const p of ['a.md', 'docs']) {
            fireEvent.keyDown(row(p), { key: 'F2' });
            fireEvent.keyDown(row(p), { key: 'Delete' });
        }
        expect(document.querySelector('input')).toBeNull();
        expect(onDelete).not.toHaveBeenCalled();
    });

    it('a folder row keeps its teal drop highlight while the drag crosses its own children (S04)', () => {
        renderTree();
        const folder = row('docs');
        fireEvent.dragOver(folder, { dataTransfer: { types: [] } });
        expect(folder.className.split(' ')).toContain('ring-teal');
        expect(folder.className).toContain('bg-teal-soft');
        expect(folder.className).not.toMatch(RETIRED_TOKENS);
        const leave = (to: Node) => {
            const evt = new Event('dragleave', { bubbles: true, cancelable: true });
            Object.defineProperty(evt, 'relatedTarget', { value: to });
            fireEvent(folder, evt);
        };
        leave(screen.getByText('docs'));
        expect(folder.className.split(' ')).toContain('ring-teal');
        leave(document.body);
        expect(folder.className.split(' ')).not.toContain('ring-teal');
    });
});

describe('ScrollArea fitWidth', () => {
    it('holds the content to the viewport width (Radix wraps it in display: table)', () => {
        const { container, unmount } = render(<ScrollArea fitWidth><div>row</div></ScrollArea>);
        expect((container.firstElementChild as HTMLElement).className).toContain('[&>[data-radix-scroll-area-viewport]>div]:!block');
        unmount();
        const plain = render(<ScrollArea><div>row</div></ScrollArea>);
        expect((plain.container.firstElementChild as HTMLElement).className).not.toContain('!block');
    });
});

it('retains the 220ms single-click window, double-click new-tab separation and immediate keyboard Enter__F0893',()=>{
 vi.useFakeTimers();const open=vi.fn(),tab=vi.fn();
 try {
 const view=render(<FileTree nodes={buildFileTree([mk('click.md')])} level={0} readOnly={false} onOpenFile={open} onOpenFileInNewTab={tab} activePath={null} onCreateFile={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onTogglePin={vi.fn()} onDropFiles={vi.fn()}/>);
 const target=row('click.md');fireEvent.click(target);vi.advanceTimersByTime(219);expect(open).not.toHaveBeenCalled();vi.advanceTimersByTime(1);expect(open).toHaveBeenCalledExactlyOnceWith('click.md');open.mockClear();
 fireEvent.click(target);fireEvent.doubleClick(target);vi.advanceTimersByTime(220);expect(open).not.toHaveBeenCalled();expect(tab).toHaveBeenCalledExactlyOnceWith('click.md');
 fireEvent.keyDown(target,{key:'Enter'});expect(open).toHaveBeenCalledExactlyOnceWith('click.md');view.unmount();
 }finally{vi.useRealTimers();}
});
