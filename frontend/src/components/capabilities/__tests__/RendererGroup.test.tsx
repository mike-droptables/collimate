import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { RendererGroup } from '../RendererGroup';
import { RENDERER_PREFS_CHANGED_EVENT, saveRendererPreferences, setRendererAvailability, type RendererEntry } from '@/lib/api';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { capabilityEntries, groupCapabilities, type CapabilityGroup } from '@/lib/capabilityList';
import { parseDefinition } from '@/lib/definitionFiles';
import { fixtureText } from '@/test/fileFixtures';
import type { VirtualFile } from '@/lib/types';
import { builtinRenderer, file, MEMO_RENDERER, renderer, seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    saveRendererPreferences: vi.fn(async () => ({})), setRendererAvailability: vi.fn(async () => ({})),
}));
vi.mock('@/lib/cellSaveBarrier', () => ({ flushCellSaves: vi.fn(async () => {}) }));

const showCell = vi.fn();
let applied: { cellId: string; files: VirtualFile[] } | null;

function mount(renderers: RendererEntry[], prefs: Record<string, string> = {}, filtering = false) {
    const group = groupCapabilities(capabilityEntries({ renderers }, () => ''), { query: '', kinds: ['renderer'] }).groups[0] as CapabilityGroup<'renderer'>;
    const slot = { data: { renderers, prefs }, error: '', retry: vi.fn() };
    return render(<RendererGroup group={group} filtering={filtering} slot={slot} onShowCell={showCell} />);
}
const row = (id: string) => document.querySelector(`[data-renderer-row="${id}"]`) as HTMLElement;
const detail = (id: string) => row(id).querySelector('[data-renderer-detail]') as HTMLElement;
const openRow = (id: string, name: string) => fireEvent.click(within(row(id)).getByRole('button', { name }));

beforeEach(() => {
    vi.clearAllMocks();
    applied = null;
    seedChannel({ applyCellFiles: vi.fn(async (cellId: string, files: VirtualFile[]) => { applied = { cellId, files }; return true; }) });
});
afterEach(cleanup);

describe('The Renderers group', () => {
    it('a name edit rewrites the renderer’s definition in the cell that holds it', async () => {
        const announced = vi.fn();
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, announced);
        try {
            mount([renderer()]);
            openRow('memo-view', '.memo Memo view');
            fireEvent.click(within(detail('memo-view')).getByText('Memo view'));
            fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Memo board' } });
            fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
            await waitFor(() => expect(applied).not.toBeNull());
            expect(flushCellSaves).toHaveBeenCalledWith('views');
            expect(applied!.cellId).toBe('views');
            const written = applied!.files.find(item => item.name === 'memo-view/memo.renderer');
            expect(parseDefinition(fixtureText(written))).toMatchObject({ id: 'memo-view', name: 'Memo board', summary: 'Shows memos.' });
            expect(applied!.files.find(item => item.name === 'memo/memo.handler')).toBeDefined();
            expect(announced).toHaveBeenCalled();
        } finally { window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, announced); }
    });

    it('refuses the write when the definition changed while it was read', async () => {
        const store = useCollimateStore.getState();
        // The cell's definition moves on between the read and the write.
        vi.spyOn(store, 'locateCell').mockImplementationOnce(() => ({ cell: { id: 'views', files: [file('memo-view/memo.renderer', MEMO_RENDERER)] } }) as never)
            .mockImplementationOnce(() => ({ cell: { id: 'views', files: [file('memo-view/memo.renderer', MEMO_RENDERER, { file_revision: 9 })] } }) as never)
            .mockImplementationOnce(() => ({ cell: { id: 'views', files: [file('memo-view/memo.renderer', MEMO_RENDERER, { file_revision: 9 })] } }) as never);
        mount([renderer()]);
        openRow('memo-view', '.memo Memo view');
        fireEvent.click(within(detail('memo-view')).getByText('Shows memos.'));
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Shows boards.' } });
        fireEvent.blur(screen.getByRole('textbox'));
        expect(await screen.findByRole('alert')).toHaveTextContent('The renderer definition changed. Retry.');
        expect(applied).toBeNull();
    });

    it('an unknown renderer id answers that its definition is unavailable', async () => {
        mount([renderer({ id: 'gone', name: 'Gone' })]);
        openRow('gone', '.memo Gone');
        fireEvent.click(within(detail('gone')).getByText('Gone'));
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Back' } });
        fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
        expect(await screen.findByRole('alert')).toHaveTextContent('The renderer definition is unavailable.');
    });

    it('turning a renderer off drops the defaults that name it first', async () => {
        mount([renderer()], { '.memo': 'memo-view', '.md': 'other' });
        fireEvent.click(within(row('memo-view')).getByRole('switch', { name: 'Memo view available' }));
        await waitFor(() => expect(setRendererAvailability).toHaveBeenCalledWith('memo-view', false));
        expect(saveRendererPreferences).toHaveBeenCalledWith({ '.md': 'other' });
        expect(vi.mocked(saveRendererPreferences).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(setRendererAvailability).mock.invocationCallOrder[0]);
    });

    it('a default chip writes the channel’s preference and shows it at once', async () => {
        mount([renderer()]);
        openRow('memo-view', '.memo Memo view');
        fireEvent.click(screen.getByRole('button', { name: 'Default renderer for .memo' }));
        await waitFor(() => expect(saveRendererPreferences).toHaveBeenCalledWith({ '.memo': 'memo-view' }));
        expect(screen.getByRole('button', { name: 'Default renderer for .memo' })).toHaveAttribute('aria-pressed', 'true');
    });

    it('a failed default save shows why, asks for a reload, and the chip goes back to the server’s value once it lands', async () => {
        vi.mocked(saveRendererPreferences).mockRejectedValueOnce(new Error('save failed'));
        const announced = vi.fn();
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, announced);
        try {
            const renderers = [renderer()];
            const group = groupCapabilities(capabilityEntries({ renderers }, () => ''), { query: '', kinds: ['renderer'] }).groups[0] as CapabilityGroup<'renderer'>;
            const view = render(<RendererGroup group={group} filtering={false} slot={{ data: { renderers, prefs: {} }, error: '', retry: vi.fn() }} onShowCell={showCell} />);
            openRow('memo-view', '.memo Memo view');
            fireEvent.click(screen.getByRole('button', { name: 'Default renderer for .memo' }));
            expect(await screen.findByRole('alert')).toHaveTextContent('save failed');
            expect(announced).toHaveBeenCalled();
            // Until the reload lands the chip shows what was written here.
            expect(screen.getByRole('button', { name: 'Default renderer for .memo' })).toHaveAttribute('aria-pressed', 'true');
            // The reload the announcement asks for brings the server's prefs, a fresh object.
            view.rerender(<RendererGroup group={group} filtering={false} slot={{ data: { renderers, prefs: {} }, error: '', retry: vi.fn() }} onShowCell={showCell} />);
            expect(screen.getByRole('button', { name: 'Default renderer for .memo' })).toHaveAttribute('aria-pressed', 'false');
        } finally { window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, announced); }
    });

    it('built-ins sit in a folded disclosure and open their card', () => {
        mount([renderer(), builtinRenderer()]);
        expect(row('plain')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Built-in (1)' }));
        openRow('plain', '.txt Plain');
        fireEvent.click(within(row('plain')).getByRole('button', { name: 'Open card' }));
        expect(showCell).toHaveBeenCalledWith('defs');
        openRow('memo-view', '.memo Memo view');
        fireEvent.click(within(row('memo-view')).getByRole('button', { name: 'Open cell' }));
        expect(showCell).toHaveBeenCalledWith('views');
    });
});
