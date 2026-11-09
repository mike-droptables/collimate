import { fixtureBodyHash } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { COLLIMATE_DND_MIME } from '@/lib/fileDrag';
// A same-cell drag over the tree background moves (the drop resolves
// same-cell to a move), so the cursor must say move. The canvas around the
// pane has its own dragover with the cross-cell rule (copy); it must not run
// after the browser's and rewrite the effect. A pane that takes no drop
// (read-only, a run folder) owns an OS file drag without accepting it
// (preventDefault with dropEffect 'none'): the browser shows no-drop and
// neither drops nor opens the file, and the canvas's NEW CELL slot never
// promises a drop the pane would swallow. A writable pane shows its own
// target instead: an inset "Add to <cell>" frame.

import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { act, render, fireEvent, screen, waitFor } from '@testing-library/react';

// The canvas harness below runs the real useCanvasFileDrop: a drop that
// reached it would create a cell through the commitImport mock. The pane's
// own import is stubbed at the ingest (it would upload to the CAS).
vi.mock('@/lib/api/transfer', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/transfer')>()),
    commitImport: vi.fn(),
}));
vi.mock('@/lib/fileIngest', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/fileIngest')>()),
    ingestFilesToVirtualFiles: vi.fn(),
}));

import { FileBrowser } from './FileBrowser';
import { FolderView } from './FolderView';

import { useCanvasFileDrop } from '@/hooks/useCanvasFileDrop';
import { commitImport } from '@/lib/api/transfer';
import { ingestFilesToVirtualFiles } from '@/lib/fileIngest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { VirtualFile } from '@/lib/types';

const mk = (name: string): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp: '2024-01-01T00:00:00Z'}));

/** An OS file drag's dataTransfer; `files` rides on the drop. */
const osFiles = () => ({
    types: ['Files'], files: [new File(['hi'], 'dropped.txt')], dropEffect: 'none',
});

/** The scene layer: the real canvas drop hook and its new-cell cue around a pane. */
function Canvas({ children }: { children: ReactNode }) {
    const { isDragging, dragProps } = useCanvasFileDrop('ch');
    return (
        <div data-testid="canvas" {...dragProps}>
            {isDragging && <div data-testid="overlay">NEW CELL</div>}
            {children}
        </div>
    );
}

beforeEach(() => {
    vi.mocked(commitImport).mockReset();
    vi.mocked(ingestFilesToVirtualFiles).mockReset();
    vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([{ ...mk('dropped.txt'), body_hash: fixtureBodyHash('hi') }]);
});
afterEach(() => useCollimateStore.getState().setFileDrag(null));

describe('FileBrowser dragover', () => {
    it('a same-cell internal drag shows move and does not reach the canvas', () => {
        const canvasDragOver = vi.fn();
        const { container } = render(
            <div onDragOver={canvasDragOver}>
                <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" />
            </div>,
        );
        useCollimateStore.getState().setFileDrag({
            source: { channel: 'ch', cellId: 'c1', nodePath: 'a.md' },
            entries: [],
        });
        const dataTransfer = { types: [COLLIMATE_DND_MIME], dropEffect: 'none' };
        const browser = (container.firstChild as HTMLElement).firstChild as HTMLElement;
        fireEvent.dragOver(browser, { dataTransfer });
        expect(dataTransfer.dropEffect).toBe('move');
        expect(canvasDragOver).not.toHaveBeenCalled();
    });

    it('a drag from a tree row or a folder tile carries {cellId, paths} on the cell-file type (RS-03)', () => {
        const dragStart = (el: Element) => {
            const dataTransfer = { types: [] as string[], setData: vi.fn(), effectAllowed: '' };
            fireEvent.dragStart(el, { dataTransfer });
            return dataTransfer.setData.mock.calls;
        };
        const files = [mk('a.md'), mk('docs/b.md'), mk('docs/c.md')];
        const tree = render(<FileBrowser files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" />);
        const row = tree.container.querySelector('[data-filenode-path="a.md"]') as HTMLElement;
        expect(dragStart(row)).toEqual([[COLLIMATE_DND_MIME, JSON.stringify({ cellId: 'c1', paths: ['a.md'] })]]);
        const folder = tree.container.querySelector('[data-filenode-path="docs"]') as HTMLElement;
        expect(dragStart(folder)).toEqual([[COLLIMATE_DND_MIME, JSON.stringify({ cellId: 'c1', paths: ['docs/b.md', 'docs/c.md'] })]]);
        tree.unmount();
        useCollimateStore.getState().setFileDrag(null);

        render(<FolderView files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" initialFolder="docs" />);
        expect(dragStart(screen.getByText('b.md').closest('[role="button"]')!)).toEqual([[COLLIMATE_DND_MIME, JSON.stringify({ cellId: 'c1', paths: ['docs/b.md'] })]]);
    });

    it('a read-only pane refuses an OS file drag without handing it to the canvas (F0848)', () => {
        const canvasDragOver = vi.fn();
        const { container } = render(
            <div onDragOver={canvasDragOver}>
                <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} readOnly cellId="c1" channel="ch" />
            </div>,
        );
        const browser = (container.firstChild as HTMLElement).firstChild as HTMLElement;
        const dataTransfer = { types: ['Files'], dropEffect: 'copy' };
        const cancelled = !fireEvent.dragOver(browser, { dataTransfer });
        // Owned, not accepted: cancelled so the browser default (open the
        // file in the tab) cannot run, with no-drop as the effect.
        expect(cancelled).toBe(true);
        expect(dataTransfer.dropEffect).toBe('none');
        expect(canvasDragOver).not.toHaveBeenCalled();
    });

    it('a writable pane takes an OS file drag itself (F0848)', () => {
        const canvasDragOver = vi.fn();
        const { container } = render(
            <div onDragOver={canvasDragOver}>
                <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" />
            </div>,
        );
        const browser = (container.firstChild as HTMLElement).firstChild as HTMLElement;
        const accepted = !fireEvent.dragOver(browser, { dataTransfer: { types: ['Files'], dropEffect: 'none' } });
        expect(accepted).toBe(true);
        expect(canvasDragOver).not.toHaveBeenCalled();
    });

    it('a Folder view inside a run folder refuses an OS file drag without handing it to the canvas (F0848)', () => {
        const canvasDragOver = vi.fn();
        const { container } = render(
            <div onDragOver={canvasDragOver}>
                <FolderView
                    files={[mk('_runs/r-1/out.txt')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}
                    cellId="c1" channel="ch" initialFolder="_runs/r-1"
                />
            </div>,
        );
        const view = (container.firstChild as HTMLElement).firstChild as HTMLElement;
        const dataTransfer = { types: ['Files'], dropEffect: 'copy' };
        const cancelled = !fireEvent.dragOver(view, { dataTransfer });
        expect(cancelled).toBe(true);
        expect(dataTransfer.dropEffect).toBe('none');
        expect(canvasDragOver).not.toHaveBeenCalled();
    });

    it('a `_runs` folder tile, and any tile of a read-only Folder view, refuses an OS file drag the same way (F0848)', () => {
        const files = [mk('_runs/r-1/out.txt'), mk('docs/a.md')];
        const canvasDragOver = vi.fn();
        const { unmount } = render(
            <div onDragOver={canvasDragOver}>
                <FolderView files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" />
            </div>,
        );
        const runs = { types: ['Files'], dropEffect: 'copy' };
        expect(fireEvent.dragOver(screen.getByTitle('_runs'), { dataTransfer: runs })).toBe(false);
        expect(runs.dropEffect).toBe('none');
        // A writable tile takes it into its folder.
        const docs = { types: ['Files'], dropEffect: 'copy' };
        expect(fireEvent.dragOver(screen.getByTitle('docs'), { dataTransfer: docs })).toBe(false);
        expect(docs.dropEffect).toBe('copy');
        expect(canvasDragOver).not.toHaveBeenCalled();
        unmount();

        render(
            <div onDragOver={canvasDragOver}>
                <FolderView files={files} onOpenFile={vi.fn()} readOnly cellId="c1" channel="ch" />
            </div>,
        );
        const roTile = { types: ['Files'], dropEffect: 'copy' };
        expect(fireEvent.dragOver(screen.getByTitle('docs'), { dataTransfer: roTile })).toBe(false);
        expect(roTile.dropEffect).toBe('none');
        expect(canvasDragOver).not.toHaveBeenCalled();
    });
});

describe('an OS file drop over a Files pane on the canvas (F0848)', () => {
    it('a bare-canvas drag raises the overlay (the harness can see it)', () => {
        render(<Canvas><div /></Canvas>);
        const canvas = screen.getByTestId('canvas');
        fireEvent.dragEnter(canvas, { dataTransfer: osFiles() });
        fireEvent.dragOver(canvas, { dataTransfer: osFiles() });
        expect(screen.getByTestId('overlay')).toBeInTheDocument();
    });

    it('over a read-only pane: no overlay, the pane consumes the drop, and no file or cell is added', async () => {
        const onUpdateFiles = vi.fn();
        render(
            <Canvas>
                <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={onUpdateFiles} readOnly cellId="c1" channel="ch" />
            </Canvas>,
        );
        const pane = screen.getByTestId('canvas').firstElementChild as HTMLElement;
        fireEvent.dragEnter(pane, { dataTransfer: osFiles() });
        fireEvent.dragOver(pane, { dataTransfer: osFiles() });
        expect(screen.queryByTestId('overlay')).toBeNull();
        expect(fireEvent.drop(pane, { dataTransfer: osFiles() })).toBe(false);
        await new Promise(r => setTimeout(r, 0));
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(ingestFilesToVirtualFiles).not.toHaveBeenCalled();
        expect(commitImport).not.toHaveBeenCalled();
    });

    it('over a run folder: no overlay, the view consumes the drop, and no file or cell is added', async () => {
        const onUpdateFiles = vi.fn();
        render(
            <Canvas>
                <FolderView
                    files={[mk('_runs/r-1/out.txt')]} onOpenFile={vi.fn()} onUpdateFiles={onUpdateFiles}
                    cellId="c1" channel="ch" initialFolder="_runs/r-1"
                />
            </Canvas>,
        );
        const view = screen.getByTestId('canvas').firstElementChild as HTMLElement;
        fireEvent.dragEnter(view, { dataTransfer: osFiles() });
        fireEvent.dragOver(view, { dataTransfer: osFiles() });
        expect(screen.queryByTestId('overlay')).toBeNull();
        expect(fireEvent.drop(view, { dataTransfer: osFiles() })).toBe(false);
        await new Promise(r => setTimeout(r, 0));
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(ingestFilesToVirtualFiles).not.toHaveBeenCalled();
        expect(commitImport).not.toHaveBeenCalled();
    });

    it('goes down when the drag leaves the window after crossing a folder row, which stops its own dragleave', () => {
        const { container } = render(
            <Canvas>
                <div data-testid="bare" />
                <FileBrowser files={[mk('docs/a.md'), mk('b.md')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" dropLabel="Cell A" />
            </Canvas>,
        );
        const bare = screen.getByTestId('bare');
        const pane = bare.nextElementSibling as HTMLElement;
        const docs = container.querySelector('[data-filenode-path="docs"]') as HTMLElement;
        const file = container.querySelector('[data-filenode-path="b.md"]') as HTMLElement;
        // Chromium's order at each step of an OS drag: enter the new element,
        // leave the old one (no relatedTarget), then the dragover.
        const step = (from: HTMLElement | null, to: HTMLElement) => {
            fireEvent.dragEnter(to, { dataTransfer: osFiles() });
            if (from) fireEvent.dragLeave(from, { dataTransfer: osFiles(), relatedTarget: null });
            fireEvent.dragOver(to, { dataTransfer: osFiles() });
        };
        step(null, bare);
        expect(screen.getByTestId('overlay')).toBeInTheDocument();
        step(bare, pane);
        step(pane, docs);
        step(docs, file);
        expect(screen.queryByTestId('overlay')).toBeNull();
        step(file, bare);
        expect(screen.getByTestId('overlay')).toBeInTheDocument();
        // Out of the window, or an Escape-cancel: Chromium leaves the last
        // element and sends no drop and no dragend.
        fireEvent.dragLeave(bare, { dataTransfer: osFiles(), relatedTarget: null });
        expect(screen.queryByTestId('overlay')).toBeNull();
        // The next drag starts from zero: one leave takes it down again.
        step(null, bare);
        expect(screen.getByTestId('overlay')).toBeInTheDocument();
        fireEvent.dragLeave(bare, { dataTransfer: osFiles(), relatedTarget: null });
        expect(screen.queryByTestId('overlay')).toBeNull();
    });

    it("over a writable pane: no overlay, and the drop reaches the pane's own import, not the canvas", async () => {
        const onUpdateFiles = vi.fn();
        render(
            <Canvas>
                <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={onUpdateFiles} cellId="c1" channel="ch" />
            </Canvas>,
        );
        const pane = screen.getByTestId('canvas').firstElementChild as HTMLElement;
        fireEvent.dragEnter(pane, { dataTransfer: osFiles() });
        fireEvent.dragOver(pane, { dataTransfer: osFiles() });
        expect(screen.queryByTestId('overlay')).toBeNull();
        expect(fireEvent.drop(pane, { dataTransfer: osFiles() })).toBe(false);
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalledTimes(1));
        const [update] = onUpdateFiles.mock.calls[0];
        expect(update([mk('a.md')]).map((f: VirtualFile) => f.name)).toEqual(['a.md', 'dropped.txt']);
        expect(commitImport).not.toHaveBeenCalled();
    });
});

describe("the in-cell drop target: a writable Files pane's inset 'Add to <cell>' frame", () => {
    const frame = () => document.querySelector('[data-in-cell-drop-frame]');

    it('an OS file drag over the tree lights it, the canvas stays dark, and the drop puts it out', async () => {
        const onUpdateFiles = vi.fn();
        render(
            <Canvas>
                <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={onUpdateFiles} cellId="c1" channel="ch" dropLabel="Cell A" />
            </Canvas>,
        );
        const pane = screen.getByTestId('canvas').firstElementChild as HTMLElement;
        fireEvent.dragEnter(pane, { dataTransfer: osFiles() });
        fireEvent.dragOver(pane, { dataTransfer: osFiles() });
        expect(screen.getByText('Add to Cell A')).toBeInTheDocument();
        expect(screen.queryByTestId('overlay')).toBeNull();
        fireEvent.drop(pane, { dataTransfer: osFiles() });
        expect(frame()).toBeNull();
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalledTimes(1));
    });

    it('stays lit across the rows (no flicker on a null relatedTarget) and goes dark once the drag has left', () => {
        const { container } = render(
            <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" dropLabel="Cell A" />,
        );
        const pane = container.firstElementChild as HTMLElement;
        const row = container.querySelector('[data-filenode-path="a.md"]') as HTMLElement;
        fireEvent.dragEnter(pane, { dataTransfer: osFiles() });
        fireEvent.dragOver(pane, { dataTransfer: osFiles() });
        // Onto a file row: Chromium enters the row, then leaves the root
        // with no relatedTarget.
        fireEvent.dragEnter(row, { dataTransfer: osFiles() });
        fireEvent.dragLeave(pane, { dataTransfer: osFiles(), relatedTarget: null });
        fireEvent.dragOver(row, { dataTransfer: osFiles() });
        expect(screen.getByText('Add to Cell A')).toBeInTheDocument();
        // Off the pane.
        fireEvent.dragLeave(row, { dataTransfer: osFiles(), relatedTarget: null });
        expect(frame()).toBeNull();
    });

    it("lit over a folder row (the row rings), dark over a `_runs` folder row, and a window dragend puts it out", () => {
        const { container } = render(
            <FileBrowser files={[mk('docs/a.md'), mk('_runs/r-1/out.txt')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" dropLabel="Cell A" />,
        );
        const pane = container.firstElementChild as HTMLElement;
        const docs = container.querySelector('[data-filenode-path="docs"]') as HTMLElement;
        const runs = container.querySelector('[data-filenode-path="_runs"]') as HTMLElement;
        fireEvent.dragEnter(pane, { dataTransfer: osFiles() });
        fireEvent.dragEnter(docs, { dataTransfer: osFiles() });
        fireEvent.dragOver(docs, { dataTransfer: osFiles() });
        expect(frame()).not.toBeNull();
        expect(docs.className).toContain('bg-teal-soft');
        fireEvent.dragEnter(runs, { dataTransfer: osFiles() });
        fireEvent.dragLeave(docs, { dataTransfer: osFiles() });
        fireEvent.dragOver(runs, { dataTransfer: osFiles() });
        expect(frame()).toBeNull();
        fireEvent.dragOver(pane, { dataTransfer: osFiles() });
        expect(frame()).not.toBeNull();
        act(() => { window.dispatchEvent(new Event('dragend')); });
        expect(frame()).toBeNull();
    });

    it('never lights for a read-only pane or a drag the app started (an internal cell-file drag)', () => {
        const { container, unmount } = render(
            <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} readOnly cellId="c1" channel="ch" dropLabel="Cell A" />,
        );
        let pane = container.firstElementChild as HTMLElement;
        fireEvent.dragEnter(pane, { dataTransfer: osFiles() });
        fireEvent.dragOver(pane, { dataTransfer: osFiles() });
        expect(frame()).toBeNull();
        unmount();

        const writable = render(
            <FileBrowser files={[mk('a.md')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" dropLabel="Cell A" />,
        );
        pane = writable.container.firstElementChild as HTMLElement;
        useCollimateStore.getState().setFileDrag({ source: { channel: 'ch', cellId: 'c2', nodePath: 'b.md' }, entries: [] });
        const internal = { types: [COLLIMATE_DND_MIME], dropEffect: 'none' };
        fireEvent.dragEnter(pane, { dataTransfer: internal });
        fireEvent.dragOver(pane, { dataTransfer: internal });
        expect(frame()).toBeNull();
    });

    it('the Folder view names the folder the files land in: the current one, or the folder tile under the drag', () => {
        const files = [mk('docs/a.md'), mk('docs/img/b.png'), mk('_runs/r-1/out.txt')];
        const { container, unmount } = render(
            <FolderView files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" dropLabel="Cell A" />,
        );
        const view = container.firstElementChild as HTMLElement;
        fireEvent.dragEnter(view, { dataTransfer: osFiles() });
        fireEvent.dragOver(view, { dataTransfer: osFiles() });
        expect(screen.getByText('Add to Cell A')).toBeInTheDocument();
        // The grid takes no teal fill for an OS drag: the frame is the cue.
        expect(container.querySelector('.bg-teal-soft')).toBeNull();
        fireEvent.dragOver(screen.getByTitle('docs'), { dataTransfer: osFiles() });
        expect(screen.getByText('Add to Cell A › docs/')).toBeInTheDocument();
        fireEvent.dragOver(screen.getByTitle('_runs'), { dataTransfer: osFiles() });
        expect(frame()).toBeNull();
        unmount();

        const inDocs = render(
            <FolderView files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} cellId="c1" channel="ch" dropLabel="Cell A" initialFolder="docs" />,
        );
        const sub = inDocs.container.firstElementChild as HTMLElement;
        fireEvent.dragEnter(sub, { dataTransfer: osFiles() });
        fireEvent.dragOver(sub, { dataTransfer: osFiles() });
        expect(screen.getByText('Add to Cell A › docs/')).toBeInTheDocument();
        fireEvent.dragLeave(sub, { dataTransfer: osFiles(), relatedTarget: null });
        expect(frame()).toBeNull();
    });

    it('a run folder view never lights it', () => {
        const { container } = render(
            <FolderView
                files={[mk('_runs/r-1/out.txt')]} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()}
                cellId="c1" channel="ch" dropLabel="Cell A" initialFolder="_runs/r-1"
            />,
        );
        const view = container.firstElementChild as HTMLElement;
        fireEvent.dragEnter(view, { dataTransfer: osFiles() });
        fireEvent.dragOver(view, { dataTransfer: osFiles() });
        expect(frame()).toBeNull();
    });
});
