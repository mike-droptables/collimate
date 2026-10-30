import { fileFixture, fixtureText } from '@/test/fileFixtures';
/**
 * The status pane (the runs pane) docked on either side of a real cell, in
 * Chromium with real CSS: the pane sits on the right edge, then across the
 * bottom, of the cell's runs dock region, and a side change never remounts
 * the renderer; its status view's rail (title and Hide status) sits on the
 * icon line the Files rail and the tab strip's toggles share; dragging the header's
 * status caret to the cell's bottom edge draws the dock band there and docks
 * the pane on release; Run's reserved slot keeps the title where it is as
 * the selection comes and goes; a live run's ink survives the unselected
 * card's quiet tools; a live run's drilldown rail keeps its name beside a
 * compact Stop at the default width; a Capabilities cell renders no status
 * pane, its panes taking the whole dock region even with an open pane
 * persisted. Run with VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    RUNS_PANE_HEIGHT, RUNS_PANE_WIDTH, createDefaultLayout, setFilesPane, setRunsPane,
} from '@/lib/cellLayout';
import { FIXTURE_PROBE_ID, type ProbeState } from '../test/rendererFixtures';
import type { Cell, Stack } from '@/lib/types';
import type { RunsPaneState } from '@/stores/collimate/types';

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
        liveRefresh: false, embed: 'native', contentHash: 'runs-probe',
    },error:null}),
}));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: () => {} }));

import CellEditor from './CellEditor';
import { StackItem } from './StackItem';

const cell: Cell = {
    id: 'cell-1', name: 'Work', pinned: 'bar.md', channel: 'ch',
    files: [fileFixture('bar.md', 'BAR', {language: 'markdown', timestamp: 't'})],
};
const stack: Stack = { id: 's1', name: 'One', cells: [cell] };
const probe = (): ProbeState => (window as unknown as { __probe: ProbeState }).__probe;
const RUN = {
    runId: 'run-a', cellId: 'cell-1', channel: 'ch', runnerId: 'runner-a', runnerName: 'Runner A',
    generatorName: 'g', label: 'runner-a-aaaaaaaa', folder: '_runs/runner-a-aaaaaaaa',
};

const seed = (runs: Partial<RunsPaneState>, filesShown = true) => setRunsPane(
    setFilesPane(createDefaultLayout({ filePaths: new Set(['bar.md']), pinnedPath: 'bar.md' }), { collapsed: !filesShown }),
    runs,
);

beforeEach(async () => {
    await page.viewport(1000, 700);
    (window as unknown as { __probe: ProbeState }).__probe = { mounts: 0, cleanups: 0, apis: [], fileUrlsAtMount: [], vis: [], set: [] };
    useCollimateStore.setState({
        cellLayouts: { 'cell-1': seed({ open: true, side: 'right' }) },
        state:{active_channel_id:'ch',channels:[{id:'ch',name:'ch',main_stacks:[stack],nav_stacks:[],main_selection_id:null,nav_selection_id:null}]}, cellCache: { [cell.id]: cell }, runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        cellDiffs: {}, runPhases: {}, lastFinishedByCell: {}, activeRunsEverywhere: [], cellLogRuns: {}, cellDisplaysReady: true,
        maximizedOverlayCellId: null, dockDrag: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [] } as never,
        runsPaneView: {}, finishedRuns: {}, annotationContexts: {}, loadCellAnnotations: async () => {},
    } as never);
});
afterEach(() => { useCollimateStore.setState({ dockDrag: null }); });

async function mountEditor(capabilitiesCell = false) {
    await render(
        <FocusProvider>
            <div style={{ position: 'relative', width: 800, height: 500, display: 'flex', flexDirection: 'column' }}>
                <CellEditor cell={cell} onUpdate={() => {}} capabilitiesCell={capabilitiesCell} />
            </div>
        </FocusProvider>,
    );
    await expect.poll(() => document.querySelector('[data-runs-dock-region]')).not.toBeNull();
    await expect.poll(() => probe().mounts, { timeout: 15000 }).toBe(1);
    const contentRegion = document.querySelector<HTMLElement>('[data-content-region]')!;
    const region = document.querySelector<HTMLElement>('[data-runs-dock-region]')!;
    const filesRow = () => region.querySelector<HTMLElement>('[data-files-dock-region]')!;
    const pane = () => region.querySelector<HTMLElement>('aside[aria-label="Status"]');
    const host = () => region.querySelector<HTMLElement>('[data-native-renderer]')!;
    return { contentRegion, region, filesRow, pane, host };
}

function Card({ selected }: { selected: boolean }) {
    return (
        <FocusProvider>
            <div style={{ position: 'relative', width: 800, height: 600 }}>
                <StackItem
                    stackId="s1" cell={cell} index={0} isFront isSelected={selected} viewMode="stack"
                    width={800} height={540} focusArea="main" onSelect={() => {}} onUpdate={() => {}}
                    onRemoveCell={() => {}} onDuplicateToNewStack={() => {}}
                />
            </div>
        </FocusProvider>
    );
}

async function mountCard(selected: boolean) {
    const view = await render(<Card selected={selected} />);
    await expect.poll(() => document.querySelector('[data-runs-dock-region]')).not.toBeNull();
    return view;
}

const dock = (side: RunsPaneState['side']) => useCollimateStore.getState().updateCellLayout('cell-1', l => setRunsPane(l, { side }));

it('the pane sits on the right, then across the bottom, of the dock region, and a side change never remounts the renderer', async () => {
    const { region, filesRow, pane, host } = await mountEditor();
    const firstHost = host();
    for (const side of ['right', 'bottom', 'right'] as const) {
        dock(side);
        await expect.poll(() => pane()?.getAttribute('data-runs-side')).toBe(side);
        const r = region.getBoundingClientRect(), p = pane()!.getBoundingClientRect(), f = filesRow().getBoundingClientRect();
        if (side === 'right') {
            expect(p.width).toBeCloseTo(RUNS_PANE_WIDTH, 0);
            expect(p.height).toBeCloseTo(r.height, 0);
            expect(p.right).toBeCloseTo(r.right, 0);
            expect(f.right).toBeCloseTo(p.left, 0);
            expect(f.left).toBeCloseTo(r.left, 0);
        } else {
            expect(p.height).toBeCloseTo(RUNS_PANE_HEIGHT, 0);
            expect(p.width).toBeCloseTo(r.width, 0);
            expect(p.bottom).toBeCloseTo(r.bottom, 0);
            expect(f.bottom).toBeCloseTo(p.top, 0);
            expect(f.top).toBeCloseTo(r.top, 0);
        }
    }
    // The same renderer host, mounted once, throughout.
    expect(host()).toBe(firstHost);
    expect(probe().mounts).toBe(1);
    expect(probe().cleanups).toBe(0);
});

it('a Capabilities cell renders no status pane: its panes fill the dock region though an open pane is persisted', async () => {
    const { region, filesRow, pane } = await mountEditor(true);
    expect(useCollimateStore.getState().cellLayouts['cell-1'].runsPane.open).toBe(true);
    expect(pane()).toBeNull();
    expect(document.querySelector('[data-runs-pane]')).toBeNull();
    const r = region.getBoundingClientRect(), f = filesRow().getBoundingClientRect();
    expect(f.left).toBeCloseTo(r.left, 0);
    expect(f.right).toBeCloseTo(r.right, 0);
    expect(f.top).toBeCloseTo(r.top, 0);
    expect(f.bottom).toBeCloseTo(r.bottom, 0);
});

it.each([true, false])('the status view\'s rail (its title and Hide status) sits on the icon line of the Files rail and the tab strip\'s toggles (files shown: %s)', async (filesShown) => {
    useCollimateStore.setState({ cellLayouts: { 'cell-1': seed({ open: true, side: 'right' }, filesShown) } });
    const { contentRegion } = await mountEditor();
    const box = (el: Element) => {
        const r = el.getBoundingClientRect();
        const c = contentRegion.getBoundingClientRect();
        return { top: r.top - c.top, bottom: r.bottom - c.bottom, height: r.height };
    };
    const button = (name: string) => document.querySelector<HTMLElement>(`button[aria-label="${name}"]`)!;
    const hide = box(button('Hide status'));
    const files = box(button(filesShown ? 'Hide files' : 'Show files'));
    const expand = box(button('Expand the cell view to the full window'));
    for (const other of [files, expand]) {
        expect(hide.top).toBeCloseTo(other.top, 1);
        expect(hide.bottom).toBeCloseTo(other.bottom, 1);
        expect(hide.height).toBeCloseTo(other.height, 1);
    }
    // The view's title stands on the same line, its baseline box ending
    // where the Hide toggle's does.
    const rail = button('Hide status').closest('[data-runs-dock-handle]')!.parentElement!;
    const title = rail.firstElementChild!.querySelector<HTMLElement>('span')!;
    expect(title.textContent).toBe('Status');
    expect(box(title).bottom).toBeCloseTo(hide.bottom, 1);
    expect(box(title).height).toBeCloseTo(hide.height, 1);
});

// A browser sends an uncaptured pointer's events to the element under it, so
// the moves and the release go there, never to the caret.
it('dragging the header\'s caret to the cell\'s bottom edge draws the band there and docks the pane on release', async () => {
    useCollimateStore.setState({ cellLayouts: { 'cell-1': seed({ open: false, side: 'right' }) } });
    await mountCard(true);
    const handle = document.querySelector<HTMLElement>('[data-cell-id="cell-1"] [data-stack-card] [data-runs-dock-handle]')!;
    const caret = handle.querySelector<HTMLElement>('[data-testid="runs-pane-toggle"]')!;
    const region = document.querySelector<HTMLElement>('[data-runs-dock-region]')!;
    expect(region.contains(caret)).toBe(false);
    const start = caret.getBoundingClientRect();
    const r = region.getBoundingClientRect();
    const at = (x: number, y: number) => ({ bubbles: true, cancelable: true, composed: true, pointerId: 7, button: 0, buttons: 1, clientX: x, clientY: y, pointerType: 'mouse', isPrimary: true });
    const under = (x: number, y: number) => document.elementFromPoint(x, y) ?? document.body;
    caret.dispatchEvent(new PointerEvent('pointerdown', at(start.left + 4, start.top + 4)));
    const ex = r.left + r.width / 2, ey = r.bottom - 10;
    expect(handle.contains(under(ex, ey))).toBe(false);
    under(ex, ey).dispatchEvent(new PointerEvent('pointermove', at(ex, ey)));
    await expect.poll(() => document.querySelector('[data-dock-preview]')).not.toBeNull();
    const band = document.querySelector<HTMLElement>('[data-dock-preview]')!;
    expect(band.getAttribute('data-dock-preview')).toBe('bottom');
    expect(band.textContent).toBe('Dock status on the bottom');
    expect(useCollimateStore.getState().dockDrag).toEqual({ cellId: 'cell-1', pane: 'runs', side: 'bottom' });
    const b = band.getBoundingClientRect();
    expect(b.height).toBeCloseTo(RUNS_PANE_HEIGHT, 0);
    expect(r.bottom - b.bottom).toBeCloseTo(4, 0);
    under(ex, ey).dispatchEvent(new PointerEvent('pointerup', at(ex, ey)));
    // The trailing click, sent to the caret itself, is swallowed.
    caret.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    await expect.poll(() => document.querySelector('aside[aria-label="Status"]')?.getAttribute('data-runs-side')).toBe('bottom');
    expect(useCollimateStore.getState().cellLayouts['cell-1'].runsPane).toMatchObject({ open: true, side: 'bottom' });
    expect(document.querySelector('[data-dock-preview]')).toBeNull();
    expect(useCollimateStore.getState().dockDrag).toBeNull();
});

it('Run\'s reserved slot keeps the title where it is, selected or not', async () => {
    const view = await mountCard(true);
    const title = () => document.querySelector<HTMLElement>('[data-stack-card] button[aria-label="Work"], [data-stack-card] [role="button"][aria-label="Work"]')
        ?? [...document.querySelectorAll<HTMLElement>('[data-stack-card] *')].find(e => e.textContent === 'Work' && e.children.length === 0)!;
    expect(document.querySelector('[data-testid="cell-run"]')).not.toBeNull();
    const selectedLeft = title().getBoundingClientRect().left;
    await view.rerender(<Card selected={false} />);
    await expect.poll(() => document.querySelector('[data-testid="cell-run"]')).toBeNull();
    expect(document.querySelector('[data-run-slot]')).not.toBeNull();
    expect(title().getBoundingClientRect().left).toBe(selectedLeft);
});

it('a live run\'s drilldown rail at the default width keeps back, the run\'s name and a compact Stop whole', async () => {
    useCollimateStore.setState({
        runs: { 'run-a': { ...RUN, runnerName: 'Smoke annotation' } },
        runsPaneView: { 'cell-1': { view: 'run', runId: 'run-a', option: 'default' } },
        refreshAnnotationContext: async () => {}, loadFileTouches: async () => {}, markFailureSeen: () => {},
    } as never);
    // The composer mounts a second renderer, so this waits on the drilldown, not on one mount.
    await render(
        <FocusProvider>
            <div style={{ position: 'relative', width: 800, height: 500, display: 'flex', flexDirection: 'column' }}>
                <CellEditor cell={cell} onUpdate={() => {}} />
            </div>
        </FocusProvider>,
    );
    const pane = () => document.querySelector<HTMLElement>('aside[aria-label="Status"]');
    await expect.poll(() => pane()?.querySelector('[data-testid="run-drilldown"]')).not.toBeNull();
    expect(pane()!.getBoundingClientRect().width).toBeCloseTo(RUNS_PANE_WIDTH, 0);
    const rail = pane()!.querySelector<HTMLElement>('[data-testid="drilldown-back"]')!.parentElement!;
    const stop = rail.querySelector<HTMLElement>('[data-testid="run-stop"]')!;
    const back = rail.querySelector<HTMLElement>('[data-testid="drilldown-back"]')!;
    const name = [...rail.querySelectorAll<HTMLElement>('span')].find(e => e.textContent === 'Smoke annotation aaaaaaaa' && e.children.length === 0)!;
    expect(stop.textContent).toBe('Stop');
    expect(stop.getAttribute('aria-label')).toBe('Stop ‘Smoke annotation’');
    // Stop keeps its content; the name gets the room left and shows.
    expect(stop.scrollWidth).toBeLessThanOrEqual(stop.clientWidth);
    expect(back.getBoundingClientRect().width).toBeGreaterThan(10);
    expect(name.getBoundingClientRect().width).toBeGreaterThan(100);
    expect(name.getBoundingClientRect().right).toBeLessThanOrEqual(stop.getBoundingClientRect().left);
});

it('a live run\'s ink holds on the unselected card\'s quiet caret', async () => {
    useCollimateStore.setState({ runs: { 'run-a': RUN } } as never);
    await mountCard(false);
    const caret = document.querySelector<HTMLElement>('[data-testid="runs-pane-toggle"]')!;
    const teal = document.createElement('span');
    teal.className = 'text-teal';
    document.body.append(teal);
    try {
        const ink = getComputedStyle(teal).color;
        expect(getComputedStyle(caret.querySelector('svg')!).color).toBe(ink);
        expect(getComputedStyle(caret.querySelector('[data-run-count]')!).color).toBe(ink);
        // The caret itself is quiet like the other tools.
        expect(getComputedStyle(caret).color).not.toBe(ink);
    } finally { teal.remove(); }
});
