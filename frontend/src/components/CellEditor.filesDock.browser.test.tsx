import { fileFixture, fixtureText } from '@/test/fileFixtures';
/**
 * The Files pane docked on either side of a real cell, in Chromium with real
 * CSS (FP-02, FP-04): the pane sits on its edge of the pane row, a side
 * change keeps the content pane (the renderer is never remounted), the
 * rail's Hide icon sits exactly where the tab strip's Show files toggle sits,
 * and dragging the Hide icon toward an edge draws the dock band on the
 * nearer of the pane's left or top edge and docks the pane there on
 * release. Run with VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { FILES_PANE_HEIGHT, FILES_PANE_WIDTH, createDefaultLayout, setFilesPane } from '@/lib/cellLayout';
import { FIXTURE_PROBE_ID, type ProbeState } from '../test/rendererFixtures';
import type { Cell, Stack } from '@/lib/types';
import type { FilesPaneSide } from '@/stores/collimate/types';

vi.mock('@/hooks/useFileDocument',()=>({useFileDocument:(_channel:string,_cellId:string,file:import('@/lib/types').VirtualFile)=>({document:null,snapshot:{body:fixtureText(file),status:'Saved',editSeq:0,savedSeq:0,localSeq:0,baseRevision:file.file_revision,recoveries:[],conflicts:[],competing:null},error:null})}));
vi.mock('@/hooks/useIdleCheckpoint', () => ({ useIdleCheckpoint: () => ({ notifyEdit: () => {}, flushCheckpoint: () => {} }) }));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    listCellVersions: async () => ({ versions: [] }),
    fetchCellHistory: async () => ({ nodes: [], total_versions: 1, current: { node_id: null, version_cell_id: 'cell-1' } }),
}));
vi.mock('@/hooks/useCellValidation', () => ({ useCellValidation: () => null }));
vi.mock('@/lib/cellFileCap', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    cellFileCap: async () => 'testcap',
}));
// The pinned file opens in the probe fixture, which counts its mounts.
vi.mock('@/hooks/useRendererId', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    useRendererId: () => ({renderer:{
        id: FIXTURE_PROBE_ID, name: 'Probe', extensions: ['.md'], supportsDiff: false,
        liveRefresh: false, embed: 'native', contentHash: 'dock-probe',
    },error:null}),
}));

import CellEditor from './CellEditor';

const cell: Cell = {
    id: 'cell-1', name: 'Work', pinned: 'bar.md',
    files: [fileFixture('bar.md', 'BAR', {language: 'markdown', timestamp: 't'})],
};
const stack: Stack = { id: 's1', name: 'One', cells: [cell] };
const probe = (): ProbeState => (window as unknown as { __probe: ProbeState }).__probe;

beforeEach(async () => {
    await page.viewport(1000, 700);
    (window as unknown as { __probe: ProbeState }).__probe = { mounts: 0, cleanups: 0, apis: [], fileUrlsAtMount: [], vis: [], set: [] };
    useCollimateStore.setState({
        cellLayouts: { 'cell-1': setFilesPane(createDefaultLayout({ filePaths: new Set(['bar.md']), pinnedPath: 'bar.md' }), { collapsed: false }) },
        state:{active_channel_id:'ch',channels:[{id:'ch',name:'ch',main_stacks:[stack],nav_stacks:[],main_selection_id:null,nav_selection_id:null}]}, cellCache: { [cell.id]: cell }, runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        maximizedOverlayCellId: null, dockDrag: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [] } as never,
    });
});
afterEach(() => { useCollimateStore.setState({ dockDrag: null }); });

async function mount() {
    await render(
        <FocusProvider>
            <div style={{ position: 'relative', width: 800, height: 500, display: 'flex', flexDirection: 'column' }}>
                <CellEditor cell={cell} onUpdate={() => {}} />
            </div>
        </FocusProvider>,
    );
    await expect.poll(() => document.querySelector('[data-files-dock-region]')).not.toBeNull();
    await expect.poll(() => probe().mounts, { timeout: 15000 }).toBe(1);
    const region = document.querySelector<HTMLElement>('[data-content-region]')!;
    const row = document.querySelector<HTMLElement>('[data-files-dock-region]')!;
    const pane = () => row.querySelector<HTMLElement>('aside[aria-label="Files"]')!;
    const content = () => row.lastElementChild as HTMLElement;
    const host = () => row.querySelector<HTMLElement>('[data-native-renderer]')!;
    return { region, row, pane, content, host };
}

const dock = (side: FilesPaneSide) => useCollimateStore.getState().updateCellLayout('cell-1', l => setFilesPane(l, { side }));

it('the pane sits on each side of the pane row, and a side change never remounts the renderer (FP-02)', async () => {
    const { row, pane, content, host } = await mount();
    const firstHost = host();
    for (const side of ['left', 'top', 'left'] as const) {
        dock(side);
        await expect.poll(() => pane().getAttribute('data-files-side')).toBe(side);
        const r = row.getBoundingClientRect(), p = pane().getBoundingClientRect(), c = content().getBoundingClientRect();
        if (side === 'left') {
            expect(p.width).toBeCloseTo(FILES_PANE_WIDTH, 0);
            expect(p.height).toBeCloseTo(r.height, 0);
            expect(p.left).toBeCloseTo(r.left, 0);
            expect(c.left).toBeCloseTo(p.right, 0);
        } else {
            expect(p.height).toBeCloseTo(FILES_PANE_HEIGHT, 0);
            expect(p.width).toBeCloseTo(r.width, 0);
            expect(p.top).toBeCloseTo(r.top, 0);
            expect(c.top).toBeCloseTo(p.bottom, 0);
        }
    }
    // The same renderer host, mounted once, throughout.
    expect(host()).toBe(firstHost);
    expect(probe().mounts).toBe(1);
    expect(probe().cleanups).toBe(0);
});

it.each(['left', 'top'] as const)('docked %s, the rail\'s Hide files icon sits exactly where the tab strip\'s Show files toggle sits (FP-02)', async (side) => {
    const { region, pane } = await mount();
    dock(side);
    await expect.poll(() => pane()?.getAttribute('data-files-side')).toBe(side);
    // Relative to the cell's content region, which neither toggle moves.
    const box = (name: string) => {
        const r = document.querySelector<HTMLElement>(`button[aria-label="${name}"]`)!.getBoundingClientRect();
        const c = region.getBoundingClientRect();
        return { top: r.top - c.top, bottom: r.bottom - c.bottom, left: r.left - c.left, width: r.width, height: r.height };
    };
    const hide = box('Hide files');
    useCollimateStore.getState().updateCellLayout('cell-1', l => setFilesPane(l, { collapsed: true }));
    await expect.poll(() => document.querySelector('button[aria-label="Show files"]')).not.toBeNull();
    const show = box('Show files');
    expect(hide.height).toBeCloseTo(show.height, 1);
    expect(hide.top).toBeCloseTo(show.top, 1);
    expect(hide.bottom).toBeCloseTo(show.bottom, 1);
    expect(hide.left).toBeCloseTo(show.left, 1);
    expect(hide.width).toBeCloseTo(show.width, 1);
});

// A browser sends an uncaptured pointer's events to the element under it, so
// the moves and the release go there, never to the icon. The first move
// sample already lands near the row's right edge, outside the icon.
it('dragging the Hide icon toward the right edge draws the band on the nearer allowed edge, the top, and docks the pane there on release (FP-04)', async () => {
    const { row, pane } = await mount();
    const handle = row.querySelector<HTMLElement>('[data-files-dock-handle]')!;
    const start = handle.getBoundingClientRect();
    const r = row.getBoundingClientRect();
    const at = (x: number, y: number) => ({ bubbles: true, cancelable: true, composed: true, pointerId: 7, button: 0, buttons: 1, clientX: x, clientY: y, pointerType: 'mouse', isPrimary: true });
    const under = (x: number, y: number) => document.elementFromPoint(x, y) ?? document.body;
    const button = handle.querySelector('button')!;
    button.dispatchEvent(new PointerEvent('pointerdown', at(start.left + 4, start.top + 4)));
    const ex = r.right - 10, ey = r.top + r.height / 2;
    expect(handle.contains(under(ex, ey))).toBe(false);
    under(ex, ey).dispatchEvent(new PointerEvent('pointermove', at(ex, ey)));
    await expect.poll(() => document.querySelector('[data-dock-preview]')).not.toBeNull();
    const band = document.querySelector<HTMLElement>('[data-dock-preview]')!;
    expect(band.getAttribute('data-dock-preview')).toBe('top');
    expect(band.textContent).toBe('Dock files on the top');
    expect(useCollimateStore.getState().dockDrag).toEqual({ cellId: 'cell-1', pane: 'files', side: 'top' });
    const b = band.getBoundingClientRect();
    expect(b.height).toBeCloseTo(FILES_PANE_HEIGHT, 0);
    expect(b.top - r.top).toBeCloseTo(4, 0);
    expect(b.left - r.left).toBeCloseTo(4, 0);
    expect(r.right - b.right).toBeCloseTo(4, 0);
    under(ex, ey).dispatchEvent(new PointerEvent('pointerup', at(ex, ey)));
    // The trailing click, sent to the button itself, is swallowed.
    button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    await expect.poll(() => pane()?.getAttribute('data-files-side')).toBe('top');
    expect(document.querySelector('[data-dock-preview]')).toBeNull();
    // The trailing click did not hide the pane.
    expect(useCollimateStore.getState().cellLayouts['cell-1'].filesPane.collapsed).toBe(false);
});

it('a press released outside the window is dropped: the next click on Hide hides, it never docks (FP-04)', async () => {
    const { row } = await mount();
    const handle = row.querySelector<HTMLElement>('[data-files-dock-handle]')!;
    const s = handle.getBoundingClientRect();
    const button = handle.querySelector('button')!;
    const at = (id: number, x: number, y: number) => ({ bubbles: true, cancelable: true, composed: true, pointerId: id, button: 0, buttons: 1, clientX: x, clientY: y, pointerType: 'mouse', isPrimary: true });
    // A press whose release never reaches the page.
    button.dispatchEvent(new PointerEvent('pointerdown', at(7, s.left + 2, s.top + 2)));
    // A plain click with a little jitter, more than the threshold from the
    // first press's origin.
    const cx = s.left + s.width / 2 + 6, cy = s.top + s.height / 2 + 6;
    button.dispatchEvent(new PointerEvent('pointerdown', at(8, cx, cy)));
    button.dispatchEvent(new PointerEvent('pointermove', at(8, cx + 1, cy)));
    button.dispatchEvent(new PointerEvent('pointerup', at(8, cx + 1, cy)));
    button.click();
    await expect.poll(() => useCollimateStore.getState().cellLayouts['cell-1'].filesPane.collapsed).toBe(true);
    expect(useCollimateStore.getState().cellLayouts['cell-1'].filesPane.side).toBe('left');
    expect(useCollimateStore.getState().dockDrag).toBeNull();
});
