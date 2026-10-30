import { fileFixture } from '@/test/fileFixtures';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { TransferPanel } from './TransferPanel';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { createDefaultLayout, setFilesPane } from '@/lib/cellLayout';
import type { Cell, VirtualFile } from '@/lib/types';
import type { StoredBlob, UploadOptions } from '@/lib/api/transfer';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/lib/api/transfer', () => ({ streamBlob: vi.fn(), expandZip: vi.fn(), commitImport: vi.fn() }));
vi.mock('@/lib/transfer/importSources', async original => ({
    ...await original<typeof import('@/lib/transfer/importSources')>(),
    pickFiles: vi.fn(), pickFolder: vi.fn(), pickZip: vi.fn(),
}));
vi.mock('@/lib/transfer/exportSinks', async original => ({
    exportFileName: (await original<typeof import('@/lib/transfer/exportSinks')>()).exportFileName,
    exportFolder: vi.fn(async () => 'saved'),
    exportZip: vi.fn(async () => 'saved'),
    folderExportAvailability: vi.fn(() => ({ available: true })),
}));

import { commitImport, expandZip, streamBlob } from '@/lib/api/transfer';
import { pickFiles, pickFolder, pickZip } from '@/lib/transfer/importSources';
import { exportFolder, exportZip, folderExportAvailability } from '@/lib/transfer/exportSinks';

/** The fake upload: a text body is filed under `t:<name>`, an image as binary. */
const upload = async (body: Blob): Promise<StoredBlob> => {
    const name = (body as File).name;
    return body.type.startsWith('image/')
        ? { blob_hash: `b:${name}`, size: body.size, encoding: 'binary' }
        : { blob_hash: `t:${name}`, size: body.size, encoding: 'utf-8' };
};
const osFile = (path: string, type = 'text/plain') => new File([path], path.split('/').pop()!, { type });

let applied: VirtualFile[] | null;
const spies = { onClose: vi.fn(), fetchState: vi.fn(async () => {}), select: vi.fn() };

function seed({ currentFolder = 'docs', history = false }: { currentFolder?: string; history?: boolean } = {}) {
    const cell = { id: 'cell', name: 'Work', files: [fileFixture('docs/old.md', 'old')] } as unknown as Cell;
    const other = { id: 'other', name: 'Other', files: [] } as unknown as Cell;
    const channel = {
        id: 'ch', name: 'Channel', nav_stacks: [], nav_selection_id: null, main_selection_id: null,
        main_stacks: [{ id: 'first', name: 'First', cells: [other] }, { id: 'stack', name: 'Notes', cells: [cell] }, { id: 'last', name: 'Last', cells: [] }],
    };
    const layout = setFilesPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }), { currentFolder });
    applied = null;
    useCollimateStore.setState({
        state: { active_channel_id: 'ch', channels: [channel] } as never,
        activeChannel: channel as never,
        cellCache: {}, cellHistory: {}, pickSessions: {},
        cellLayouts: { cell: layout },
        applyCellFiles: vi.fn(async (_id: string, next: VirtualFile[]) => { applied = next; return true; }),
        fetchState: spies.fetchState,
        requestSelectCell: spies.select,
    } as never);
    useCollimateStore.getState().openPickSession('cell', 'transfer');
    if (history) useCollimateStore.setState({ cellHistory: { cell: {} } } as never);
}

const panel = () => screen.getByRole('dialog', { name: 'Import / export' });
const primary = () => screen.getByTestId('cell-panel-primary');
const rowNames = () => screen.getAllByRole('option').map(o => o.querySelector('div')!.textContent);
const modeButton = (label: string) => within(screen.getByRole('group', { name: 'Mode' })).getByRole('button', { name: label });
const key = (k: string, opts: Partial<KeyboardEventInit> = {}) => fireEvent.keyDown(panel(), { key: k, ...opts });
const mount = () => render(<TransferPanel cellId="cell" onClose={spies.onClose} />);

beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(streamBlob).mockImplementation(upload);
    vi.mocked(commitImport).mockResolvedValue({ stack_ids: ['s-new'], cell_ids: ['c-new'] });
    vi.mocked(folderExportAvailability).mockReturnValue({ available: true });
    seed();
});
afterEach(cleanup);

describe('Transfer panel: modes, rows and keyboard (TR-11)', () => {
    it('is the cell panel of kind transfer, opening on Import with its three rows', () => {
        mount();
        expect(panel()).toHaveAttribute('data-cell-panel', 'transfer');
        expect(within(screen.getByRole('group', { name: 'Mode' })).getAllByRole('button').map(b => b.textContent)).toEqual(['Import', 'Export']);
        expect(rowNames()).toEqual(['Files…', 'Folder…', 'Zip…']);
        expect(primary()).toHaveTextContent('Choose files…');
        expect(screen.getAllByRole('option')[0]).toHaveTextContent('Into ‘docs’ of this cell');
    });

    it('arrows move the row, Tab switches mode, Esc closes', () => {
        mount();
        key('ArrowDown');
        expect(primary()).toHaveTextContent('Choose folder…');
        key('ArrowDown');
        expect(primary()).toHaveTextContent('Choose zip…');
        key('Tab');
        expect(modeButton('Export')).toHaveAttribute('aria-pressed', 'true');
        expect(rowNames()).toEqual(['This cell', 'This stack', 'All cells and stacks']);
        expect(screen.getAllByRole('option')[1]).toHaveTextContent('Notes/ with a folder per cell');
        key('Escape');
        expect(spies.onClose).toHaveBeenCalled();
    });

    it('on a read-only cell Import is greyed with its reason and the panel shows Export', () => {
        seed({ history: true });
        mount();
        expect(modeButton('Import')).toBeDisabled();
        expect(modeButton('Import')).toHaveAttribute('title', 'Leave version history to import into this cell.');
        expect(modeButton('Export')).toHaveAttribute('aria-pressed', 'true');
        expect(rowNames()).toEqual(['This cell', 'This stack', 'All cells and stacks']);
    });

    it('a current folder inside a run refuses Files… with the reason', () => {
        seed({ currentFolder: '_runs/r-1' });
        mount();
        expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-disabled', 'true');
        expect(primary()).toBeDisabled();
        expect(primary()).toHaveAttribute('title', expect.stringContaining('belongs to a run'));
    });
});

describe('Transfer panel: import (TR-11, TR-07)', () => {
    it('Files… streams the picked files up and lands them in the current folder by hash', async () => {
        vi.mocked(pickFiles).mockResolvedValue([osFile('a.md'), osFile('pic.png', 'image/png')]);
        mount();
        key('Enter');
        await waitFor(() => expect(spies.onClose).toHaveBeenCalled());
        expect(pickFiles).toHaveBeenCalledTimes(1);
        expect(streamBlob).toHaveBeenCalledTimes(2);
        expect(applied?.map(f => [f.name, f.encoding, f.body_hash])).toEqual([
            ['docs/old.md', 'utf-8', expect.any(String)],
            ['docs/a.md', 'utf-8', 't:a.md'],
            ['docs/pic.png', 'binary', 'b:pic.png'],
        ]);
        expect(applied?.some(f => 'content' in f && f.name !== 'docs/old.md')).toBe(false);
    });

    it('a closed picker imports nothing', async () => {
        vi.mocked(pickFiles).mockResolvedValue(null);
        mount();
        key('Enter');
        await act(async () => {});
        expect(streamBlob).not.toHaveBeenCalled();
        expect(spies.onClose).not.toHaveBeenCalled();
    });

    const TREE = {
        rootName: 'proj', rootIsFolder: true, dirs: ['empty'], unreadable: [],
        files: ['notes.md', 'src/a.ts', 'src/b.ts', 'web/home/index.html'].map(path => ({ path, file: osFile(path) })),
    };
    const openFolderStep = async () => {
        vi.mocked(pickFolder).mockResolvedValue(TREE);
        mount();
        key('ArrowDown');
        key('Enter');
        await screen.findByRole('radiogroup', { name: 'Split level' });
    };
    const previewText = () => screen.getByRole('list', { name: 'Preview' }).textContent;

    it('Folder… opens the step: destination, the valid split levels only, and the preview', async () => {
        await openFolderStep();
        expect(screen.getByText(/Import ‘proj’ · 4 files/)).toBeInTheDocument();
        expect(within(screen.getByRole('radiogroup', { name: 'Destination' })).getAllByRole('radio')).toHaveLength(2);
        const levels = within(screen.getByRole('radiogroup', { name: 'Split level' })).getAllByRole('radio');
        expect(levels.map(r => r.closest('label')!.textContent)).toEqual(['One cell', 'Each top folder a cell', 'Top folders are stacks']);
        expect(previewText()).toBe('projproj4 files');
        fireEvent.click(screen.getByLabelText('Each top folder a cell'));
        expect(previewText()).toBe('projproj1 filesrc2 filesweb1 fileempty0 files');
        fireEvent.click(screen.getByLabelText('Top folders are stacks'));
        expect(previewText()).toBe('projproj1 filesrcsrc2 fileswebhome1 file');
    });

    it('Import creates the chosen stacks and cells after this stack, then selects the first', async () => {
        await openFolderStep();
        fireEvent.click(screen.getByLabelText('Each top folder a cell'));
        fireEvent.click(screen.getByRole('button', { name: 'Import' }));
        await waitFor(() => expect(spies.onClose).toHaveBeenCalled());
        expect(streamBlob).toHaveBeenCalledTimes(4);
        expect(commitImport).toHaveBeenCalledWith('ch', {
            insert_index: 2,
            stacks: [{ name: 'proj', cells: [
                { name: 'proj', files: [{ path: 'notes.md', blob_hash: 't:notes.md' }], dirs: [] },
                { name: 'src', files: [{ path: 'a.ts', blob_hash: 't:a.ts' }, { path: 'b.ts', blob_hash: 't:b.ts' }], dirs: [] },
                { name: 'web', files: [{ path: 'home/index.html', blob_hash: 't:index.html' }], dirs: [] },
                { name: 'empty', files: [], dirs: [] },
            ] }],
        }, expect.anything());
        expect(spies.fetchState).toHaveBeenCalled();
        expect(spies.select).toHaveBeenCalledWith('c-new');
    });

    it('a run folder inside a split cell is listed under Left out and neither uploads nor reaches the commit (TR-08)', async () => {
        vi.mocked(pickFolder).mockResolvedValue({
            rootName: 'proj', rootIsFolder: true, dirs: ['work/.collrun'], unreadable: [],
            files: ['notes.md', 'work/x.txt', 'work/_runs/r-1/out.txt'].map(path => ({ path, file: osFile(path) })),
        });
        mount();
        key('ArrowDown');
        key('Enter');
        await screen.findByRole('radiogroup', { name: 'Split level' });
        // One cell keeps the folder whole: nothing in it is reserved there.
        expect(screen.queryByRole('heading', { name: 'Left out' })).toBeNull();
        fireEvent.click(screen.getByLabelText('Each top folder a cell'));
        const leftOut = screen.getByRole('heading', { name: 'Left out' }).parentElement!;
        expect(leftOut).toHaveTextContent('work/_runs/r-1/out.txt — Reserved for runs');
        expect(leftOut).toHaveTextContent('work/.collrun — Reserved for runs');
        expect(previewText()).toBe('projproj1 filework1 file');
        fireEvent.click(screen.getByRole('button', { name: 'Import' }));
        await waitFor(() => expect(spies.onClose).toHaveBeenCalled());
        expect(vi.mocked(streamBlob).mock.calls.map(([body]) => (body as File).name)).toEqual(['notes.md', 'x.txt']);
        expect(commitImport).toHaveBeenCalledWith('ch', {
            insert_index: 2,
            stacks: [{ name: 'proj', cells: [
                { name: 'proj', files: [{ path: 'notes.md', blob_hash: 't:notes.md' }], dirs: [] },
                { name: 'work', files: [{ path: 'x.txt', blob_hash: 't:x.txt' }], dirs: [] },
            ] }],
        }, expect.anything());
    });

    it('into the current folder the folder keeps its name, and Enter imports', async () => {
        await openFolderStep();
        fireEvent.click(screen.getByLabelText(/Into ‘docs’ of this cell/));
        expect(screen.queryByRole('radiogroup', { name: 'Split level' })).toBeNull();
        expect(screen.getByText(/land in ‘proj’ inside ‘docs’/)).toBeInTheDocument();
        // The step keeps the keyboard on its choices: Enter imports.
        expect(document.activeElement).toHaveAttribute('type', 'radio');
        fireEvent.keyDown(document.activeElement!, { key: 'Enter' });
        await waitFor(() => expect(applied).not.toBeNull());
        expect(applied!.map(f => f.name)).toEqual([
            'docs/old.md', 'docs/proj/notes.md', 'docs/proj/src/a.ts', 'docs/proj/src/b.ts', 'docs/proj/web/home/index.html', 'docs/proj/empty/.collkeep',
        ]);
        expect(commitImport).not.toHaveBeenCalled();
    });

    it('Esc in the step goes back to the rows', async () => {
        await openFolderStep();
        // The step takes the keyboard on its first choice in an effect after
        // the render that showed it; Esc is pressed there, not wherever focus
        // still is on a slow run.
        const first = within(screen.getByRole('radiogroup', { name: 'Destination' })).getAllByRole('radio')[0];
        await waitFor(() => expect(first).toHaveFocus());
        fireEvent.keyDown(first, { key: 'Escape' });
        expect(screen.queryAllByRole('radiogroup')).toEqual([]);
        expect(rowNames()).toEqual(['Files…', 'Folder…', 'Zip…']);
        expect(spies.onClose).not.toHaveBeenCalled();
    });

    it('Zip… expands the zip on the server; its members import without a second upload', async () => {
        vi.mocked(pickZip).mockResolvedValue(new File(['PK'], 'bundle.zip', { type: 'application/zip' }));
        vi.mocked(expandZip).mockResolvedValue({
            entries: [{ path: 'Main/One/a.txt', blob_hash: 'z1', size: 1, encoding: 'utf-8' }, { path: 'Main/Two/b.bin', blob_hash: 'z2', size: 2, encoding: 'binary' }],
            dirs: [], skipped: [{ path: '../evil', reason: 'Path escapes the archive' }],
        });
        mount();
        key('ArrowUp');
        key('Enter');
        await screen.findByRole('radiogroup', { name: 'Split level' });
        expect(expandZip).toHaveBeenCalledWith(expect.any(File), expect.objectContaining({ signal: expect.any(AbortSignal) }));
        expect(screen.getByText(/Path escapes the archive/)).toBeInTheDocument();
        fireEvent.click(screen.getByLabelText('Top folders are stacks'));
        fireEvent.click(screen.getByRole('button', { name: 'Import' }));
        await waitFor(() => expect(commitImport).toHaveBeenCalled());
        expect(streamBlob).not.toHaveBeenCalled();
        expect(vi.mocked(commitImport).mock.calls[0][1].stacks).toEqual([{ name: 'Main', cells: [
            { name: 'One', files: [{ path: 'a.txt', blob_hash: 'z1' }], dirs: [] },
            { name: 'Two', files: [{ path: 'b.bin', blob_hash: 'z2' }], dirs: [] },
        ] }]);
    });

    it('shows progress while uploading, and Cancel stops the import', async () => {
        vi.mocked(pickFiles).mockResolvedValue([osFile('big.bin')]);
        vi.mocked(streamBlob).mockImplementation((body: Blob, options: UploadOptions = {}) => new Promise((_resolve, reject) => {
            options.onProgress?.(body.size / 2, body.size);
            options.signal?.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')));
        }));
        mount();
        key('Enter');
        const bar = await screen.findByRole('progressbar');
        await waitFor(() => expect(bar).toHaveAttribute('aria-valuenow', '50'));
        fireEvent.click(within(screen.getByTestId('transfer-progress')).getByRole('button', { name: 'Cancel' }));
        await waitFor(() => expect(screen.queryByRole('progressbar')).toBeNull());
        expect(applied).toBeNull();
        expect(spies.onClose).not.toHaveBeenCalled();
        expect(screen.queryByRole('alert')).toBeNull();
    });

    it('is a drop target: dropped OS files open the step, a lone zip goes to the server', async () => {
        mount();
        const files = [osFile('a.md'), osFile('b.md')];
        fireEvent.drop(panel(), { dataTransfer: { types: ['Files'], files, dropEffect: 'none' } });
        await screen.findByRole('radiogroup', { name: 'Destination' });
        expect(screen.getByText(/Import ‘Dropped files’ · 2 files/)).toBeInTheDocument();

        cleanup();
        seed();
        vi.mocked(expandZip).mockResolvedValue({ entries: [{ path: 'x.txt', blob_hash: 'z', size: 1, encoding: 'utf-8' }], dirs: [], skipped: [] });
        mount();
        fireEvent.drop(panel(), { dataTransfer: { types: ['Files'], files: [new File(['PK'], 'drop.zip')], dropEffect: 'none' } });
        await screen.findByText(/Import ‘drop’ · 1 file/);
        expect(expandZip).toHaveBeenCalledTimes(1);
    });
});

describe('Transfer panel: export (TR-10, TR-11)', () => {
    it('exports this cell to a folder; Shift+Enter switches to zip, named after the scope', async () => {
        mount();
        key('Tab');
        expect(primary()).toHaveTextContent('Export to folder…');
        key('Enter');
        await waitFor(() => expect(exportFolder).toHaveBeenCalledWith('ch', { scope: 'cell', cellId: 'cell' }, expect.objectContaining({ signal: expect.any(AbortSignal) })));
        expect(await screen.findByRole('status')).toHaveTextContent('Exported ‘Work’ into the folder.');
        key('Enter', { shiftKey: true });
        expect(primary()).toHaveTextContent('Export as zip…');
        key('ArrowDown');
        key('Enter');
        await waitFor(() => expect(exportZip).toHaveBeenCalledWith('ch', { scope: 'stack', stackId: 'stack' }, 'Notes', expect.anything()));
        key('ArrowDown');
        key('Enter');
        await waitFor(() => expect(exportZip).toHaveBeenCalledWith('ch', { scope: 'all' }, 'Channel', expect.anything()));
    });

    it('without a folder picker the export is a zip, Folder is disabled and the reason shows', () => {
        vi.mocked(folderExportAvailability).mockReturnValue({ available: false, reason: 'No folder access here.' });
        mount();
        key('Tab');
        expect(primary()).toHaveTextContent('Export as zip…');
        expect(screen.getByTestId('cell-panel-secondary')).toBeDisabled();
        expect(screen.getByTestId('cell-panel-note')).toHaveTextContent('No folder access here.');
    });

    it("a failed export shows the server's sentence in the error line", async () => {
        vi.mocked(exportFolder).mockRejectedValueOnce(new Error('Unknown cell'));
        mount();
        key('Tab');
        key('Enter');
        expect(await screen.findByRole('alert')).toHaveTextContent('Unknown cell');
    });
});
