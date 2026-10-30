// @vitest-environment jsdom
// Paste into the Files pane (FP-05): a window capture-phase `paste` listener
// acting while focus is inside the surface and not in an editable; images,
// text and one OS file go through the filename prompt, several files land
// under their names, the internal clipboard goes to pasteInto.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useRef } from 'react';
import { fixtureBodyHash, fileFixture } from '@/test/fileFixtures';
import type { VirtualFile } from '@/lib/types';
import type { FilesUpdater } from '@/lib/fileTransfer';
import type { PasteData } from '@/lib/clipboardPaste';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));
// Every pasted body streams into the CAS; the fake answers the server's
// sniff (binary for an image).
vi.mock('@/lib/api/transfer', () => ({
    streamBlob: vi.fn(async (body: Blob) => {
        const text = await body.text();
        const binary = body.type.startsWith('image/');
        return { blob_hash: binary ? `bin:${body.size}` : fixtureBodyHash(text), size: body.size, encoding: binary ? 'binary' : 'utf-8' };
    }),
}));

import { PASTE_FROM_KEYBOARD_HINT, useFilesPanePaste } from './useFilesPanePaste';
import { streamBlob } from '@/lib/api/transfer';
import { useCollimateStore } from '@/stores/useCollimateStore';

let cell: VirtualFile[] = [];
const write = vi.fn(async (update: FilesUpdater) => { cell = update(cell); });
const pasteInto = vi.fn();
const openFile = vi.fn();
let folder = 'docs';

function Surface({ readOnly = false, cellId = 'cell' }: { readOnly?: boolean; cellId?: string }) {
    const ref = useRef<HTMLDivElement>(null);
    const paste = useFilesPanePaste({
        containerRef: ref, targetFolder: () => folder, files: cell, cellId, readOnly,
        onUpdateFiles: write, onOpenFile: openFile, pasteInto,
    });
    return (
        <>
            <div ref={ref} tabIndex={-1} data-testid="surface">
                <div tabIndex={0} data-testid="row">docs</div>
                <input aria-label="rename" />
                <button type="button" onClick={() => void paste.pasteHere(folder)}>menu paste</button>
                {paste.prompt}
            </div>
            <div tabIndex={0} data-testid="elsewhere" />
        </>
    );
}

/** A synthetic paste event aimed at `target` (the browser aims it at the
 *  selection; the hook reads focus, not the target). */
function paste(content: { files?: File[]; images?: File[]; text?: string }, target: EventTarget = document.body): boolean {
    const files = content.files ?? [];
    const data: PasteData = {
        files,
        items: [...files, ...(content.images ?? [])].map(file => ({ kind: 'file', type: file.type, getAsFile: () => file })),
        getData: format => (format === 'text/plain' ? content.text ?? '' : ''),
    };
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: data });
    act(() => { target.dispatchEvent(event); });
    return event.defaultPrevented;
}

const focusRow = () => act(() => screen.getByTestId('row').focus());
const nameField = () => screen.getByPlaceholderText('filename') as HTMLInputElement;
const vf = (name: string, body = name) => fileFixture(name, body, { language: 'text', timestamp: 't' });

beforeEach(() => {
    cell = [vf('docs/old.md')];
    folder = 'docs';
    write.mockClear();
    pasteInto.mockClear();
    openFile.mockClear();
    vi.mocked(streamBlob).mockClear();
    useCollimateStore.setState({ fileClipboard: null, copiedOsText: null, confirmDialog: null });
});
afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    Reflect.deleteProperty(navigator, 'clipboard');
});

describe('useFilesPanePaste (FP-05)', () => {
    it('an image prompts for a name, then streams up and lands as a binary .png that opens (FP-05)', async () => {
        render(<Surface />);
        focusRow();
        expect(paste({ images: [new File(['PNGDATA'], '', { type: 'image/png' })] })).toBe(true);
        expect(screen.getByText(/Paste image in/)).toBeTruthy();
        expect(nameField().value).toMatch(/^pasted-image-\d{8}-\d{6}\.png$/);
        const filename = nameField().value;
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        await waitFor(() => expect(openFile).toHaveBeenCalledWith(`docs/${filename}`));
        expect(vi.mocked(streamBlob)).toHaveBeenCalledTimes(1);
        expect(vi.mocked(streamBlob).mock.calls[0][0].type).toBe('image/png');
        expect(cell.find(f => f.name === `docs/${filename}`)).toMatchObject({ encoding: 'binary', body_hash: 'bin:7' });
        expect(screen.queryByPlaceholderText('filename')).toBeNull();
    });

    it('a screenshot named image.png by the browser is an image paste too (FP-05)', () => {
        render(<Surface />);
        focusRow();
        paste({ files: [new File(['PNG'], 'image.png', { type: 'image/png' })] });
        expect(nameField().value).toMatch(/^pasted-image-/);
    });

    it('text lands as .md when it reads as markdown, else .txt (FP-05)', async () => {
        render(<Surface />);
        focusRow();
        paste({ text: '# Notes\n\n- one' });
        expect(screen.getByText(/Paste text in/)).toBeTruthy();
        const md = `docs/${nameField().value}`;
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        await waitFor(() => expect(openFile).toHaveBeenCalledWith(md));
        expect(cell.find(f => f.name === md)).toMatchObject({ encoding: 'utf-8', body_hash: fixtureBodyHash('# Notes\n\n- one') });

        focusRow();
        paste({ text: 'just words' });
        fireEvent.change(nameField(), { target: { value: 'words.txt' } });
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        await waitFor(() => expect(openFile).toHaveBeenCalledWith('docs/words.txt'));
    });

    it('one OS file is prefilled with its own name (FP-05)', async () => {
        render(<Surface />);
        focusRow();
        paste({ files: [new File(['%PDF'], 'report.pdf', { type: 'application/pdf' })] });
        expect(screen.getByText(/Paste file in/)).toBeTruthy();
        expect(nameField().value).toBe('report.pdf');
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        await waitFor(() => expect(openFile).toHaveBeenCalledWith('docs/report.pdf'));
    });

    it('a name taken while the body uploads is refused at commit, and nothing is written (FP-05)', async () => {
        vi.mocked(streamBlob).mockImplementationOnce(async (body: Blob) => {
            cell = [...cell, vf('docs/race.txt', 'theirs')];
            return { blob_hash: fixtureBodyHash(await body.text()), size: body.size, encoding: 'utf-8' };
        });
        render(<Surface />);
        focusRow();
        paste({ text: 'mine' });
        fireEvent.change(nameField(), { target: { value: 'race.txt' } });
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        expect(await screen.findByText('`docs/race.txt` already exists')).toBeTruthy();
        expect(cell.filter(f => f.name === 'docs/race.txt')).toHaveLength(1);
        expect(cell.find(f => f.name === 'docs/race.txt')!.body_hash).toBe(fixtureBodyHash('theirs'));
        expect(openFile).not.toHaveBeenCalled();
    });

    it('several files land under their names after one overwrite question (FP-05)', async () => {
        const confirm = vi.spyOn(useCollimateStore.getState(), 'openConfirm').mockResolvedValue(true);
        render(<Surface />);
        focusRow();
        paste({ files: [new File(['new old'], 'old.md'), new File(['b'], 'b.txt'), new File(['c'], 'c.txt')] });
        await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
        expect(confirm).toHaveBeenCalledTimes(1);
        expect(confirm.mock.calls[0][0].body).toContain('docs/old.md');
        expect(cell.map(f => f.name)).toEqual(['docs/old.md', 'docs/b.txt', 'docs/c.txt']);
        expect(cell[0].body_hash).toBe(fixtureBodyHash('new old'));
        expect(screen.queryByPlaceholderText('filename')).toBeNull();
        expect(openFile).not.toHaveBeenCalled();
    });

    it('declining the overwrite question writes nothing (FP-05)', async () => {
        const confirm = vi.spyOn(useCollimateStore.getState(), 'openConfirm').mockResolvedValue(false);
        render(<Surface />);
        focusRow();
        paste({ files: [new File(['x'], 'old.md'), new File(['b'], 'b.txt')] });
        await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
        await Promise.resolve();
        expect(write).not.toHaveBeenCalled();
        expect(vi.mocked(streamBlob)).not.toHaveBeenCalled();
    });

    it('the internal clipboard\'s marker text routes to pasteInto (FP-05)', () => {
        useCollimateStore.setState({ fileClipboard: {
            op: 'copy', label: "'a.md'", entries: [], source: { channel: 'c', cellId: 'x', nodePath: 'a.md' }, osText: 'a.md',
        } });
        render(<Surface />);
        focusRow();
        expect(paste({ text: 'a.md' })).toBe(true);
        expect(pasteInto).toHaveBeenCalledExactlyOnceWith('docs');
        expect(screen.queryByPlaceholderText('filename')).toBeNull();
    });

    it('after a cut is pasted, its path on the OS clipboard pastes nothing (FP-05)', async () => {
        useCollimateStore.getState().setFileClipboard({
            op: 'cut', label: "'a.md'", entries: [], source: { channel: 'c', cellId: 'x', nodePath: 'a.md' }, osText: 'a.md',
        });
        render(<Surface />);
        focusRow();
        expect(paste({ text: 'a.md' })).toBe(true);
        expect(pasteInto).toHaveBeenCalledExactlyOnceWith('docs');
        // useFileTransfer's pasteInto clears the clipboard once the cut moved.
        act(() => useCollimateStore.getState().clearFileClipboard());
        focusRow();
        expect(paste({ text: 'a.md' })).toBe(false);
        expect(screen.queryByPlaceholderText('filename')).toBeNull();
        expect(pasteInto).toHaveBeenCalledTimes(1);

        const alert = vi.spyOn(useCollimateStore.getState(), 'openAlert').mockResolvedValue(true);
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
            read: vi.fn(async () => [{ types: ['text/plain'], getType: async () => new Blob(['a.md']) }]),
        } });
        fireEvent.click(screen.getByText('menu paste'));
        await waitFor(() => expect(alert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Nothing to paste' })));
        expect(screen.queryByPlaceholderText('filename')).toBeNull();
        expect(write).not.toHaveBeenCalled();
    });

    it('the prompt refuses a run path and the folder marker name, as New file does (FP-05)', async () => {
        folder = '';
        render(<Surface />);
        focusRow();
        paste({ files: [new File(['x'], '_runs')] });
        expect(nameField().value).toBe('_runs');
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        expect(await screen.findByText(/belong to runs/)).toBeTruthy();
        fireEvent.change(nameField(), { target: { value: '.collkeep' } });
        fireEvent.keyDown(nameField(), { key: 'Enter' });
        expect(await screen.findByText('Reserved name')).toBeTruthy();
        expect(vi.mocked(streamBlob)).not.toHaveBeenCalled();
        expect(write).not.toHaveBeenCalled();
        expect(openFile).not.toHaveBeenCalled();
    });

    it('several files skip a run path and a folder marker and say why (FP-05)', async () => {
        folder = '';
        const alert = vi.spyOn(useCollimateStore.getState(), 'openAlert').mockResolvedValue(true);
        render(<Surface />);
        focusRow();
        paste({ files: [new File(['k'], '.collkeep'), new File(['r'], '_runs'), new File(['b'], 'b.txt')] });
        await waitFor(() => expect(alert).toHaveBeenCalledTimes(1));
        expect(cell.map(f => f.name)).toEqual(['docs/old.md', 'b.txt']);
        expect(alert.mock.calls[0][0]).toMatchObject({ title: '2 files were not pasted' });
        expect(alert.mock.calls[0][0].body).toMatch(/reserved/);
        expect(alert.mock.calls[0][0].body).not.toMatch(/already in the cell/);
    });

    it('nothing happens in a rename field, outside the surface, in a read-only cell or under `_runs/` (FP-05)', () => {
        const view = render(<Surface />);
        act(() => screen.getByLabelText('rename').focus());
        expect(paste({ text: 'words' })).toBe(false);
        act(() => screen.getByTestId('elsewhere').focus());
        expect(paste({ text: 'words' })).toBe(false);
        focusRow();
        folder = '_runs/r-1';
        expect(paste({ text: 'words' })).toBe(false);
        folder = 'docs';
        view.rerender(<Surface readOnly />);
        focusRow();
        expect(paste({ text: 'words' })).toBe(false);
        view.rerender(<Surface cellId="" />);
        focusRow();
        expect(paste({ text: 'words' })).toBe(false);
        expect(screen.queryByPlaceholderText('filename')).toBeNull();
        expect(pasteInto).not.toHaveBeenCalled();
    });

    it('Paste here takes the internal clipboard first, else reads the system clipboard (FP-05)', async () => {
        const read = vi.fn(async () => [{ types: ['text/plain'], getType: async () => new Blob(['from the menu']) }]);
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { read } });
        useCollimateStore.setState({ fileClipboard: {
            op: 'cut', label: "'a.md'", entries: [], source: { channel: 'c', cellId: 'x', nodePath: 'a.md' }, osText: 'a.md',
        } });
        render(<Surface />);
        fireEvent.click(screen.getByText('menu paste'));
        await waitFor(() => expect(pasteInto).toHaveBeenCalledWith('docs'));
        expect(read).not.toHaveBeenCalled();

        act(() => useCollimateStore.setState({ fileClipboard: null }));
        fireEvent.click(screen.getByText('menu paste'));
        expect(await screen.findByText(/Paste text in/)).toBeTruthy();
        expect(read).toHaveBeenCalledTimes(1);
    });

    it('Paste here explains Ctrl+V when the browser refuses the clipboard (FP-05)', async () => {
        const alert = vi.spyOn(useCollimateStore.getState(), 'openAlert').mockResolvedValue(true);
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
            read: vi.fn(async () => { throw new DOMException('Read permission denied.', 'NotAllowedError'); }),
        } });
        render(<Surface />);
        fireEvent.click(screen.getByText('menu paste'));
        await waitFor(() => expect(alert).toHaveBeenCalledWith(expect.objectContaining({ body: PASTE_FROM_KEYBOARD_HINT })));
        expect(PASTE_FROM_KEYBOARD_HINT).toBe('Press Ctrl+V in the files pane to paste from the system clipboard');
    });
});
