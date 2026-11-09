import { fileFixture, fixtureText } from '@/test/fileFixtures';
/**
 * The annotation pane docked on either side of a real cell, in Chromium with
 * real CSS: the pane sits on the right edge, then across the bottom, of the
 * cell's runs dock region, and a side change never remounts the renderer;
 * its rail (the run picker and Hide annotation) sits on the icon line the
 * Files rail and the tab strip's toggles share; the annotation toggle stands
 * on the tab strip's line directly left of the expand button (the header
 * carries none), and dragging it to the cell's bottom edge draws the dock
 * band there and docks the pane on release; the status pill keeps one width (header and
 * strip sizes alike, a back card's slot the same), so the title stays where
 * it is as the selection, the runs and the section's state come and go; a
 * live run's pill keeps its ink on the unselected card, while the
 * annotation toggle stays plain; a live run's drilldown rail keeps its name beside a compact
 * Stop at the default width; a Capabilities cell renders no annotation pane,
 * its panes taking the whole dock region even with an open pane persisted;
 * the status section opens above the tab strip on the card and in the
 * full-window view alike, the full window's strip and its pill moving down
 * under it, and a wheel over it scrolls its own list and never activates
 * the leaf whose strip carries the pill.
 * Run with VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page, userEvent } from 'vitest/browser';
import '@/app/globals.css';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    RUNS_PANE_HEIGHT, RUNS_PANE_WIDTH, allLeaves, createDefaultLayout, openEmptyTab, setActiveLeaf,
    setFilesPane, setRunsPane, splitWithTab, topRightLeaf,
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
import { StatusPillSlot, StatusToggle } from './cellEditor/StatusToggle';

const cell: Cell = {
    id: 'cell-1', name: 'Work', pinned: 'bar.md', channel: 'ch',
    files: [fileFixture('bar.md', 'BAR', {language: 'markdown', timestamp: 't'})],
};
const stack: Stack = { id: 's1', name: 'One', cells: [cell] };
const probe = (): ProbeState => (window as unknown as { __probe: ProbeState }).__probe;
// A skin token (--line, --line-2) as the computed colour a border shows.
function tokenColor(token: string): string {
    const el = document.createElement('i');
    el.style.borderColor = `var(${token})`;
    document.body.appendChild(el);
    try { return getComputedStyle(el).borderTopColor; } finally { el.remove(); }
}
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
        annotationPaneView: {}, finishedRuns: {}, annotationContexts: {}, loadCellAnnotations: async () => {},
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
    const pane = () => region.querySelector<HTMLElement>('aside[aria-label="Annotation"]');
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
        await expect.poll(() => pane()?.getAttribute('data-annotation-side')).toBe(side);
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

it('a Capabilities cell renders no annotation pane: its panes fill the dock region though an open pane is persisted', async () => {
    const { region, filesRow, pane } = await mountEditor(true);
    expect(useCollimateStore.getState().cellLayouts['cell-1'].runsPane.open).toBe(true);
    expect(pane()).toBeNull();
    expect(document.querySelector('[data-annotation-pane]')).toBeNull();
    const r = region.getBoundingClientRect(), f = filesRow().getBoundingClientRect();
    expect(f.left).toBeCloseTo(r.left, 0);
    expect(f.right).toBeCloseTo(r.right, 0);
    expect(f.top).toBeCloseTo(r.top, 0);
    expect(f.bottom).toBeCloseTo(r.bottom, 0);
});

it.each([true, false])('the pane\'s rail (the run picker and Hide annotation) sits on the icon line of the Files rail and the tab strip\'s toggles (files shown: %s)', async (filesShown) => {
    useCollimateStore.setState({ cellLayouts: { 'cell-1': seed({ open: true, side: 'right' }, filesShown) } });
    const { contentRegion } = await mountEditor();
    const box = (el: Element) => {
        const r = el.getBoundingClientRect();
        const c = contentRegion.getBoundingClientRect();
        return { top: r.top - c.top, bottom: r.bottom - c.bottom, height: r.height };
    };
    const button = (name: string) => document.querySelector<HTMLElement>(`button[aria-label="${name}"]`)!;
    const hide = box(button('Hide annotation'));
    const files = box(button(filesShown ? 'Hide files' : 'Show files'));
    const expand = box(button('Expand the cell view to the full window'));
    for (const other of [files, expand]) {
        expect(hide.top).toBeCloseTo(other.top, 1);
        expect(hide.bottom).toBeCloseTo(other.bottom, 1);
        expect(hide.height).toBeCloseTo(other.height, 1);
    }
    // The run picker stands on the same line, its box ending where the
    // Hide toggle's does.
    const rail = button('Hide annotation').closest('[data-runs-dock-handle]')!.parentElement!;
    const picker = rail.firstElementChild!.querySelector<HTMLElement>('[data-testid="annotation-run-picker"]')!;
    expect(picker.textContent).toBe('All');
    expect(box(picker).bottom).toBeCloseTo(hide.bottom, 1);
    expect(box(picker).height).toBeCloseTo(hide.height, 1);
});

// A browser sends an uncaptured pointer's events to the element under it, so
// the moves and the release go there, never to the toggle.
it('the tab strip\'s annotation toggle stands left of the expand button; dragging it to the cell\'s bottom edge draws the band there and docks the pane on release', async () => {
    useCollimateStore.setState({ cellLayouts: { 'cell-1': seed({ open: false, side: 'right' }) } });
    await mountCard(true);
    const card = document.querySelector<HTMLElement>('[data-cell-id="cell-1"] [data-stack-card]')!;
    const toggles = card.querySelectorAll<HTMLElement>('[data-testid="annotation-pane-toggle"]');
    expect(toggles).toHaveLength(1);
    const toggle = toggles[0];
    const handle = toggle.closest<HTMLElement>('[data-runs-dock-handle]')!;
    const region = document.querySelector<HTMLElement>('[data-runs-dock-region]')!;
    // On the strip, inside the dock region, never in the header.
    expect(region.contains(toggle)).toBe(true);
    const expand = card.querySelector<HTMLElement>('button[aria-label="Expand the cell view to the full window"]')!;
    expect(handle.nextElementSibling).toBe(expand);
    const start = toggle.getBoundingClientRect();
    const e = expand.getBoundingClientRect();
    expect(start.right).toBeLessThanOrEqual(e.left + 0.5);
    expect(start.top).toBeCloseTo(e.top, 1);
    expect(start.bottom).toBeCloseTo(e.bottom, 1);
    const r = region.getBoundingClientRect();
    const at = (x: number, y: number) => ({ bubbles: true, cancelable: true, composed: true, pointerId: 7, button: 0, buttons: 1, clientX: x, clientY: y, pointerType: 'mouse', isPrimary: true });
    const under = (x: number, y: number) => document.elementFromPoint(x, y) ?? document.body;
    toggle.dispatchEvent(new PointerEvent('pointerdown', at(start.left + 4, start.top + 4)));
    const ex = r.left + r.width / 2, ey = r.bottom - 10;
    expect(handle.contains(under(ex, ey))).toBe(false);
    under(ex, ey).dispatchEvent(new PointerEvent('pointermove', at(ex, ey)));
    await expect.poll(() => document.querySelector('[data-dock-preview]')).not.toBeNull();
    const band = document.querySelector<HTMLElement>('[data-dock-preview]')!;
    expect(band.getAttribute('data-dock-preview')).toBe('bottom');
    expect(band.textContent).toBe('Dock annotation on the bottom');
    expect(useCollimateStore.getState().dockDrag).toEqual({ cellId: 'cell-1', pane: 'runs', side: 'bottom' });
    const b = band.getBoundingClientRect();
    expect(b.height).toBeCloseTo(RUNS_PANE_HEIGHT, 0);
    expect(r.bottom - b.bottom).toBeCloseTo(4, 0);
    under(ex, ey).dispatchEvent(new PointerEvent('pointerup', at(ex, ey)));
    // The trailing click, sent to the toggle itself, is swallowed.
    toggle.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    await expect.poll(() => document.querySelector('aside[aria-label="Annotation"]')?.getAttribute('data-annotation-side')).toBe('bottom');
    expect(useCollimateStore.getState().cellLayouts['cell-1'].runsPane).toMatchObject({ open: true, side: 'bottom' });
    expect(document.querySelector('[data-dock-preview]')).toBeNull();
    expect(useCollimateStore.getState().dockDrag).toBeNull();
});

it('the status pill keeps one width and the title where it is: selected or not, idle or running, closed or open', async () => {
    const view = await mountCard(true);
    const title = () => document.querySelector<HTMLElement>('[data-stack-card] button[aria-label="Work"], [data-stack-card] [role="button"][aria-label="Work"]')
        ?? [...document.querySelectorAll<HTMLElement>('[data-stack-card] *')].find(e => e.textContent === 'Work' && e.children.length === 0)!;
    const pill = () => document.querySelector<HTMLElement>('[data-testid="cell-status-toggle"]')!;
    const content = () => pill().querySelector<HTMLElement>('[data-pill-content]')!;
    expect(pill().getAttribute('data-pill')).toBe('run');
    expect(pill().hasAttribute('data-selected')).toBe(true);
    // No fill in any state; the stronger (line-2) border alone marks the
    // selected cell, the hairline the rest (the pointer is off the card).
    const TRANSPARENT = 'rgba(0, 0, 0, 0)';
    expect(getComputedStyle(pill()).backgroundColor).toBe(TRANSPARENT);
    const strongEdge = getComputedStyle(pill()).borderTopColor;
    expect(strongEdge).toBe(tokenColor('--line-2'));
    const width = pill().getBoundingClientRect().width;
    const left = title().getBoundingClientRect().left;
    expect(width).toBeGreaterThan(40);
    // Nothing spills: the widest content fits the sizer's width.
    expect(content().scrollWidth).toBeLessThanOrEqual(content().clientWidth);
    await view.rerender(<Card selected={false} />);
    await expect.poll(() => pill().hasAttribute('data-selected')).toBe(false);
    // The card's hover lifts the border too, and an earlier test may have
    // left the real pointer over the card: park it in the window's corner.
    const corner = document.createElement('div');
    corner.style.cssText = 'position:fixed;right:0;bottom:0;width:8px;height:8px';
    document.body.appendChild(corner);
    await userEvent.hover(corner);
    corner.remove();
    // Settled after the colour transition: the hairline.
    await expect.poll(() => getComputedStyle(pill()).borderTopColor).toBe(tokenColor('--line'));
    expect(pill().getBoundingClientRect().width).toBe(width);
    expect(title().getBoundingClientRect().left).toBe(left);
    useCollimateStore.setState({ runs: { 'run-a': RUN, 'run-b': { ...RUN, runId: 'run-b', label: 'smoke-b' } } } as never);
    await expect.poll(() => content().textContent).toBe('2 running');
    expect(pill().getBoundingClientRect().width).toBe(width);
    expect(title().getBoundingClientRect().left).toBe(left);
    useCollimateStore.getState().toggleStatusSection('cell-1');
    await expect.poll(() => pill().querySelector('[data-chevron]')?.getAttribute('data-chevron')).toBe('up');
    expect(getComputedStyle(pill()).backgroundColor).toBe(TRANSPARENT);
    expect(pill().getBoundingClientRect().width).toBe(width);
    expect(title().getBoundingClientRect().left).toBe(left);
    useCollimateStore.setState({ runs: {}, statusSection: {} } as never);
});

it('every pill state measures one width at each size, and a back card\'s slot measures the pill\'s', async () => {
    const runs = Object.fromEntries(Array.from({ length: 99 }, (_, i) => [`run-${i}`, { ...RUN, runId: `run-${i}`, label: `smoke-${i}` }]));
    await render(
        <div>
            {(['header', 'strip'] as const).map(variant => (
                <div key={variant} data-variant={variant}>
                    <div data-case="run"><StatusToggle cellId="idle" variant={variant} canRun selected /></div>
                    <div data-case="quiet"><StatusToggle cellId="idle" variant={variant} canRun selected={false} /></div>
                    <div data-case="status"><StatusToggle cellId="idle" variant={variant} canRun={false} selected /></div>
                    <div data-case="running"><StatusToggle cellId="cell-1" variant={variant} canRun selected /></div>
                    <div data-case="slot"><StatusPillSlot cellId="idle" variant={variant} /></div>
                </div>
            ))}
        </div>,
    );
    useCollimateStore.setState({ runs } as never);
    const box = (variant: string, c: string) => document.querySelector<HTMLElement>(`[data-variant="${variant}"] [data-case="${c}"] ${c === 'slot' ? '[data-run-slot]' : 'button'}`)!;
    await expect.poll(() => box('header', 'running').querySelector('[data-pill-content]')?.textContent).toBe('99 running');
    for (const variant of ['header', 'strip']) {
        const width = box(variant, 'run').getBoundingClientRect().width;
        const height = box(variant, 'run').getBoundingClientRect().height;
        // 1.375rem on the header, the strip tools' 1.125rem line on the strip.
        const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
        expect(height).toBeCloseTo((variant === 'strip' ? 1.125 : 1.375) * rem, 0);
        for (const c of ['quiet', 'status', 'running', 'slot']) {
            expect(box(variant, c).getBoundingClientRect().width).toBe(width);
            expect(box(variant, c).getBoundingClientRect().height).toBe(height);
        }
        // The widest content, 99 running, fits without spilling.
        const content = box(variant, 'running').querySelector<HTMLElement>('[data-pill-content]')!;
        expect(content.scrollWidth).toBeLessThanOrEqual(content.clientWidth);
    }
    expect(box('strip', 'run').getBoundingClientRect().width).toBeLessThan(box('header', 'run').getBoundingClientRect().width);
    useCollimateStore.setState({ runs: {} } as never);
});

it('a live run\'s drilldown rail at the default width keeps the picker\'s run name and a compact Stop whole', async () => {
    useCollimateStore.setState({
        runs: { 'run-a': { ...RUN, runnerName: 'Smoke annotation' } },
        annotationPaneView: { 'cell-1': { view: 'run', runId: 'run-a', option: 'default' } },
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
    const pane = () => document.querySelector<HTMLElement>('aside[aria-label="Annotation"]');
    await expect.poll(() => pane()?.querySelector('[data-testid="run-drilldown"]')).not.toBeNull();
    expect(pane()!.getBoundingClientRect().width).toBeCloseTo(RUNS_PANE_WIDTH, 0);
    const picker = pane()!.querySelector<HTMLElement>('[data-testid="annotation-run-picker"]')!;
    const rail = picker.parentElement!;
    const stop = rail.querySelector<HTMLElement>('[data-testid="run-stop"]')!;
    const name = [...picker.querySelectorAll<HTMLElement>('span')].find(e => e.textContent === 'Smoke annotation aaaaaaaa' && e.children.length === 0)!;
    expect(stop.textContent).toBe('Stop');
    expect(stop.getAttribute('aria-label')).toBe('Stop ‘Smoke annotation’');
    // Stop keeps its content; the name gets the room left and shows.
    expect(stop.scrollWidth).toBeLessThanOrEqual(stop.clientWidth);
    expect(name.getBoundingClientRect().width).toBeGreaterThan(100);
    expect(name.getBoundingClientRect().right).toBeLessThanOrEqual(stop.getBoundingClientRect().left);
});

it('a live run\'s pill keeps its ink on the unselected card; the annotation toggle stays plain', async () => {
    useCollimateStore.setState({ runs: { 'run-a': RUN } } as never);
    await mountCard(false);
    const pill = document.querySelector<HTMLElement>('[data-testid="cell-status-toggle"]')!;
    const toggle = document.querySelector<HTMLElement>('[data-testid="annotation-pane-toggle"]')!;
    const teal = document.createElement('span');
    teal.className = 'text-teal';
    document.body.append(teal);
    try {
        const ink = getComputedStyle(teal).color;
        // "1 running" and its chevron, in the light's ink.
        expect(pill.querySelector('[data-pill-content]')!.textContent).toBe('1 running');
        expect(getComputedStyle(pill.querySelector('[data-pill-count]')!).color).toBe(ink);
        expect(getComputedStyle(pill.querySelector('[data-chevron]')!).color).toBe(ink);
        // The annotation toggle counts nothing and takes no run colour.
        expect(toggle.querySelector('[data-pill-count]')).toBeNull();
        expect(getComputedStyle(toggle.querySelector('svg')!).color).toBe(getComputedStyle(toggle).color);
        expect(getComputedStyle(toggle).color).not.toBe(ink);
    } finally { teal.remove(); }
});

// The status section opens above the tab strip on the card and in the
// full-window view alike: the pane region's first band, the region's width.
const tabStrip = () => document.querySelector<HTMLElement>('button[aria-label="New tab"]')!.parentElement!.parentElement!;
const statusSectionEl = () => document.querySelector<HTMLElement>('[data-testid="status-section"]');

it('on the card the status section opens above the tab strip, its bottom on the strip\'s top', async () => {
    useCollimateStore.setState({ statusSection: {} } as never);
    const { contentRegion } = await mountEditor();
    useCollimateStore.getState().toggleStatusSection('cell-1');
    await expect.poll(() => statusSectionEl()).not.toBeNull();
    const s = statusSectionEl()!.getBoundingClientRect();
    expect(s.top).toBeCloseTo(contentRegion.getBoundingClientRect().top, 0);
    expect(s.width).toBeCloseTo(contentRegion.getBoundingClientRect().width, 0);
    expect(s.bottom).toBeCloseTo(tabStrip().getBoundingClientRect().top, 0);
    useCollimateStore.setState({ statusSection: {} } as never);
});

it('in the full-window view the status section opens above the strip, the region\'s width, the strip and its pill moving down under it; a wheel over it scrolls its own list and activates no leaf', async () => {
    // Enough live runs that the section's list overflows its 40% cap.
    const runs = Object.fromEntries(Array.from({ length: 24 }, (_, at) => {
        const runId = `run-${String(at).padStart(2, '0')}`;
        return [runId, { ...RUN, runId, label: `runner-a-${runId}`, folder: `_runs/runner-a-${runId}` }];
    }));
    // Split side by side, the pinned file's leaf (left) active, so the
    // top-right leaf that carries the pill is not: a wheel reaching that
    // leaf's handlers would make it the active one.
    let layout = seed({ open: true, side: 'right' });
    layout = openEmptyTab(layout, layout.activeLeafId);
    const empty = allLeaves(layout.root)[0].tabs.find(t => t.kind === 'empty')!;
    layout = splitWithTab(layout, layout.activeLeafId, empty.id, 'right');
    const toolsLeaf = topRightLeaf(layout.root).id;
    const fileLeaf = allLeaves(layout.root).find(l => l.id !== toolsLeaf)!.id;
    layout = setActiveLeaf(layout, fileLeaf);
    useCollimateStore.setState({ statusSection: {}, runs, cellLayouts: { 'cell-1': layout } } as never);
    await mountEditor();
    document.querySelector<HTMLElement>('button[aria-label="Expand the cell view to the full window"]')!.click();
    const pill = () => document.querySelector<HTMLElement>('[data-full-window] [data-testid="cell-status-toggle-strip"]');
    await expect.poll(() => pill()).not.toBeNull();
    await expect.poll(() => document.querySelector('[data-full-window] [data-native-renderer]')).not.toBeNull();
    const host = () => document.querySelector<HTMLElement>('[data-full-window] [data-native-renderer]')!;
    const region = document.querySelector<HTMLElement>('[data-full-window] [data-pane-region]')!;
    const stripBefore = tabStrip().getBoundingClientRect();
    const pillBefore = pill()!.getBoundingClientRect();
    const hostTopBefore = host().getBoundingClientRect().top;
    expect(stripBefore.top).toBeCloseTo(region.getBoundingClientRect().top, 0);

    pill()!.click();
    await expect.poll(() => statusSectionEl()).not.toBeNull();
    const section = statusSectionEl()!;
    expect(section.closest('[data-full-window]')).not.toBeNull();
    expect(pill()!.getAttribute('aria-expanded')).toBe('true');
    const r = region.getBoundingClientRect();
    const s = section.getBoundingClientRect();
    // The region's top band, its whole width (over the annotation pane's
    // column too), no taller than 40% of it.
    expect(s.top).toBeCloseTo(r.top, 0);
    expect(s.left).toBeCloseTo(r.left, 0);
    expect(s.right).toBeCloseTo(r.right, 0);
    expect(s.height).toBeLessThanOrEqual(r.height * 0.4 + 0.5);
    expect(s.right).toBeGreaterThanOrEqual(document.querySelector<HTMLElement>('[data-full-window] aside[aria-label="Annotation"]')!.getBoundingClientRect().right - 0.5);
    // The strip, its pill and the tab body move down by the section's height.
    const strip = tabStrip().getBoundingClientRect();
    expect(strip.top).toBeCloseTo(s.bottom, 0);
    expect(strip.top - stripBefore.top).toBeCloseTo(s.height, 0);
    const pillAfter = pill()!.getBoundingClientRect();
    expect(pillAfter.left).toBeCloseTo(pillBefore.left, 0);
    expect(pillAfter.top - pillBefore.top).toBeCloseTo(s.height, 0);
    await expect.poll(() => host().getBoundingClientRect().top).toBeCloseTo(hostTopBefore + s.height, 0);

    // A wheel over the section scrolls its own list; the active leaf stays
    // as it was (the section sits in no leaf).
    let list = section.querySelector<HTMLElement>('[data-testid="status-run-row"]')!;
    while (getComputedStyle(list).overflowY !== 'auto') list = list.parentElement!;
    expect(section.contains(list)).toBe(true);
    expect(section.querySelectorAll('[data-testid="status-run-row"]')).toHaveLength(24);
    expect(list.scrollHeight).toBeGreaterThan(list.clientHeight);
    const activeLeafId = () => useCollimateStore.getState().cellLayouts['cell-1'].activeLeafId;
    expect(activeLeafId()).toBe(fileLeaf);
    await page.elementLocator(list).wheel({ delta: { y: 120 } });
    await expect.poll(() => list.scrollTop).toBeGreaterThan(0);
    expect(activeLeafId()).toBe(fileLeaf);
    expect(tabStrip().getBoundingClientRect().top).toBeCloseTo(strip.top, 0);
    // The same wheel over the pill's strip does reach that leaf.
    await page.elementLocator(pill()!.parentElement!).wheel({ delta: { y: 120 } });
    await expect.poll(() => activeLeafId()).toBe(toolsLeaf);

    // The pill where it now sits closes it, and everything moves back up.
    pill()!.click();
    await expect.poll(() => statusSectionEl()).toBeNull();
    expect(pill()!.getAttribute('aria-expanded')).toBe('false');
    expect(tabStrip().getBoundingClientRect().top).toBe(stripBefore.top);
    expect(pill()!.getBoundingClientRect().toJSON()).toEqual(pillBefore.toJSON());
    await expect.poll(() => host().getBoundingClientRect().top).toBeCloseTo(hostTopBefore, 0);
    useCollimateStore.setState({ statusSection: {}, runs: {} } as never);
});
