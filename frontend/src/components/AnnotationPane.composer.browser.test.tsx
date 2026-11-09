import { fileFixture, fixtureText } from '@/test/fileFixtures';
/**
 * The annotation pane's drilldown and its inline composer in a real cell, in
 * Chromium with real CSS, docked on the right (280px) and across the bottom
 * (200px): the view's rail (the run picker) on the icon line, the options
 * under it, the timeline filling the pane down to its bottom with the add
 * row, then the composer, at its end; nothing overflows the pane; Expand
 * covers the view's body with the composer; ten messages and the composer
 * scroll together inside the timeline only; and, the composer's editor
 * being a pooled frame, the frame follows its box as the log scrolls,
 * the scroll itself waking the pool (no missed wake), hidden while the box
 * reaches past the log (nothing in the log clips the frame). The editor is the
 * native probe renderer, or an iframe one for the pooled frame. Run with
 * VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import { useCallback } from 'react';
import { FocusProvider } from '@/contexts/FocusContext';
import { GuestPoolProvider, useGuestPool } from '@/contexts/GuestPoolContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { createDefaultLayout, setRunsPane } from '@/lib/cellLayout';
import { FIXTURE_PROBE_ID, type ProbeState } from '../test/rendererFixtures';
import type { Cell, Stack } from '@/lib/types';
import type { RunsPaneState, AnnotationPaneView } from '@/stores/collimate/types';
import type { AnnotationMessage } from '@/lib/annotations';

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
// Every file, the composer's draft included, opens in the probe: native,
// or an iframe renderer (a pooled frame) where a test asks.
const host = vi.hoisted(() => ({ embed: 'native' as 'native' | 'iframe' }));
vi.mock('@/hooks/useRendererId', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    useRendererId: () => ({renderer:{
        id: FIXTURE_PROBE_ID, name: 'Probe', extensions: ['.md'], supportsDiff: false,
        liveRefresh: false, embed: host.embed, contentHash: 'composer-probe',
    },error:null}),
}));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: () => {} }));
vi.mock('@/hooks/useRunInbox', () => ({ useRunInbox: () => ({ cards: [], status: 'open', answer: () => false }) }));

import CellEditor from './CellEditor';

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
const message = (i: number): AnnotationMessage => ({
    id: `m${i}`, context_id: RUN.runId, parent_id: null, author: 'run', author_label: RUN.label, kind: 'message',
    body: `A reply number ${i}, long enough to wrap onto a second line in a narrow pane`, context_items: [], refs: [],
    mode: 'queued', position: i, held: false, revision: 1, created_at: `2026-10-24 10:${String(i).padStart(2, '0')}:00`,
    edited_at: null, read_at: `2026-10-24 10:${String(i).padStart(2, '0')}:00`,
});
const context = (n: number) => ({
    id: RUN.runId, cell_id: 'cell-1', run_id: RUN.runId, label: RUN.label, bar_on: false, listening: true,
    interrupt_seq: 0, rev: 1, sealed: false, messages: Array.from({ length: n }, (_, i) => message(i)),
});

const seed = (runs: Partial<RunsPaneState>) => setRunsPane(createDefaultLayout({ filePaths: new Set(['bar.md']), pinnedPath: 'bar.md' }), runs);

/** The pool's overlay, as the scene registers it (a ruler-less, fixed
 *  origin: overlay coordinates are the viewport's). */
function PoolOverlay() {
    const pool = useGuestPool();
    const ref = useCallback((element: HTMLDivElement | null) => pool.registerOverlay(element), [pool]);
    return <div ref={ref} data-pool-overlay style={{ position: 'fixed', left: 0, top: 0, width: 0, height: 0 }} />;
}

async function mount(runs: Partial<RunsPaneState>, view: AnnotationPaneView, { messages = 2, composerOpen = true, pooled = false } = {}) {
    await page.viewport(1000, 700);
    host.embed = pooled ? 'iframe' : 'native';
    (window as unknown as { __probe: ProbeState }).__probe = { mounts: 0, cleanups: 0, apis: [], fileUrlsAtMount: [], vis: [], set: [] };
    useCollimateStore.setState({
        cellLayouts: { 'cell-1': seed(runs) },
        state: { active_channel_id: 'ch', channels: [{ id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [], main_selection_id: null, nav_selection_id: null }] },
        cellCache: { [cell.id]: cell }, runs: { [RUN.runId]: RUN }, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        cellDiffs: {}, runPhases: {}, lastFinishedByCell: {}, activeRunsEverywhere: [], cellLogRuns: {}, cellDisplaysReady: true,
        maximizedOverlayCellId: null, dockDrag: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [] } as never,
        annotationPaneView: { 'cell-1': view }, finishedRuns: {}, fileTouches: { [RUN.runId]: [] },
        composerDrafts: {}, statusSection: {}, statusSectionFocus: {},
        annotationContexts: { 'cell-1': { [RUN.runId]: context(messages) } },
        loadCellAnnotations: async () => {}, refreshAnnotationContext: async () => {}, loadFileTouches: async () => {},
    } as never);
    const editor = (
        <div style={{ position: 'relative', width: 800, height: 500, display: 'flex', flexDirection: 'column' }}>
            <CellEditor cell={cell} onUpdate={() => {}} />
        </div>
    );
    await render(
        <FocusProvider>
            {pooled ? <GuestPoolProvider><PoolOverlay />{editor}</GuestPoolProvider> : editor}
        </FocusProvider>,
    );
    await expect.poll(() => document.querySelector('aside[aria-label="Annotation"]')).not.toBeNull();
    const pane = document.querySelector<HTMLElement>('aside[aria-label="Annotation"]')!;
    if (composerOpen) {
        await expect.poll(() => document.querySelector('[data-testid="composer-add"]')).not.toBeNull();
        q('composer-add').click();
        await expect.poll(() => document.querySelector('[data-testid="annotation-composer"]')).not.toBeNull();
    }
    return { pane };
}

const rect = (el: Element) => el.getBoundingClientRect();
const q = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;

/** No descendant of the pane reaches past its sides. */
function fitsAcross(pane: HTMLElement) {
    const p = rect(pane);
    for (const el of pane.querySelectorAll<HTMLElement>('*')) {
        const r = rect(el);
        if (r.width === 0 || getComputedStyle(el).display === 'none') continue;
        if (el.closest('[data-runs-dock-handle]') || el.getAttribute('role') === 'separator') continue;
        expect(r.left, el.outerHTML.slice(0, 80)).toBeGreaterThanOrEqual(p.left - 0.5);
        expect(r.right, el.outerHTML.slice(0, 80)).toBeLessThanOrEqual(p.right + 0.5);
    }
}

beforeEach(() => {
    useCollimateStore.setState({ dockDrag: null });
});

it.each([
    ['right', 280],
    ['bottom', 200],
] as const)('docked %s, the drilldown stacks rail, options and timeline down the pane, the add row then the composer at its end (%dpx)', async (side, size) => {
    const { pane } = await mount({ open: true, side }, { view: 'run', runId: RUN.runId, option: 'default' }, { composerOpen: false });
    const p = rect(pane);
    expect(side === 'right' ? p.width : p.height).toBeCloseTo(size, 0);
    const hide = rect(document.querySelector('button[aria-label="Hide annotation"]')!);
    const picker = rect(q('annotation-run-picker'));
    expect(picker.bottom).toBeCloseTo(hide.bottom, 0);
    const rail = q('annotation-run-picker').closest('.h-\\[1\\.875rem\\]')!;
    const options = q('drilldown-option-default').parentElement!;
    expect(rect(options).top).toBeCloseTo(rect(rail).bottom, 0);
    const timeline = rect(q('annotation-timeline'));
    expect(timeline.top).toBeGreaterThanOrEqual(rect(options).bottom - 0.5);
    expect(timeline.bottom).toBeCloseTo(p.bottom, 0);
    // The add row ends the log, inside it.
    const add = rect(q('composer-add'));
    expect(add.left).toBeGreaterThanOrEqual(timeline.left);
    expect(add.right).toBeLessThanOrEqual(timeline.right + 0.5);
    expect(q('annotation-timeline').lastElementChild).toBe(q('composer-add'));
    fitsAcross(pane);
    // Opened, the composer takes the add row's place, whole in view, its
    // editor box and controls inside it.
    q('composer-add').click();
    await expect.poll(() => document.querySelector('[data-testid="annotation-composer"]')).not.toBeNull();
    await expect.poll(() => probe().mounts, { timeout: 15000 }).toBeGreaterThanOrEqual(2);
    const log = q('annotation-timeline');
    expect(log.lastElementChild).toBe(q('annotation-composer'));
    const composer = rect(q('annotation-composer'));
    expect(composer.bottom).toBeLessThanOrEqual(rect(log).bottom + 0.5);
    expect(composer.top).toBeGreaterThanOrEqual(rect(log).top - 0.5);
    const box = rect(document.querySelector('[data-annotation-composer-box]')!);
    const controls = rect(q('composer-send').parentElement!);
    expect(box.bottom).toBeCloseTo(controls.top, 0);
    expect(box.height).toBeGreaterThan(40);
    expect(controls.bottom).toBeLessThanOrEqual(composer.bottom);
    fitsAcross(pane);
});

it('Expand covers the view\'s body with the composer; Collapse puts it back in the log', async () => {
    const { pane } = await mount({ open: true, side: 'right' }, { view: 'run', runId: RUN.runId, option: 'default' });
    const before = rect(document.querySelector('[data-annotation-composer-box]')!).height;
    q('composer-expand').click();
    await expect.poll(() => q('annotation-composer').getAttribute('data-expanded')).toBe('true');
    const body = rect(q('annotation-timeline').closest('[role="tabpanel"]')!);
    const composer = rect(q('annotation-composer'));
    for (const edge of ['top', 'bottom', 'left', 'right'] as const) expect(composer[edge]).toBeCloseTo(body[edge], 0);
    expect(composer.bottom).toBeCloseTo(rect(pane).bottom, 0);
    // It covers the timeline (painted over it), the rail and options still above.
    const mid = document.elementFromPoint(composer.left + composer.width / 2, composer.top + 4)!;
    expect(q('annotation-composer').contains(mid)).toBe(true);
    expect(rect(q('drilldown-option-default')).bottom).toBeLessThanOrEqual(composer.top + 0.5);
    const expanded = rect(document.querySelector('[data-annotation-composer-box]')!);
    expect(expanded.height).toBeGreaterThan(before + 150);
    q('composer-expand').click();
    await expect.poll(() => q('annotation-composer').getAttribute('data-expanded')).toBe('false');
    expect(rect(document.querySelector('[data-annotation-composer-box]')!).height).toBeCloseTo(before, 0);
    expect(q('annotation-timeline').lastElementChild).toBe(q('annotation-composer'));
});

it('ten messages and the composer scroll together inside the timeline alone', async () => {
    const { pane } = await mount({ open: true, side: 'right' }, { view: 'run', runId: RUN.runId, option: 'default' }, { messages: 10 });
    await expect.poll(() => document.querySelectorAll('[data-testid="annotation-message"]').length).toBe(10);
    const timeline = q('annotation-timeline');
    expect(timeline.scrollHeight).toBeGreaterThan(timeline.clientHeight);
    // Opening it scrolled it into view, at the log's end.
    await expect.poll(() => rect(q('annotation-composer')).bottom <= rect(timeline).bottom + 0.5).toBe(true);
    const composerTop = rect(q('annotation-composer')).top;
    const from = timeline.scrollTop;
    timeline.scrollTop = from - 40;
    await expect.poll(() => timeline.scrollTop).toBe(from - 40);
    expect(rect(q('annotation-composer')).top).toBeCloseTo(composerTop + 40, 0);
    expect(pane.scrollHeight).toBeLessThanOrEqual(pane.clientHeight + 1);
});

/** The y of a pooled element's `translate(...)`, as the sync pass wrote it
 *  (the overlay sits at the viewport's origin, scale 1). */
const elementY = (el: HTMLElement) => {
    const m = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px/.exec(el.style.transform);
    return m ? Number(m[2]) : NaN;
};
const sync = () => window.__collimateGuestSync!;

it('the composer\'s pooled frame follows its box as the log scrolls, the scroll waking the pool, and hides while the box leaves the log', async () => {
    // Thirty messages: the log must overflow by well over the shift below
    // on any font metrics.
    await mount({ open: true, side: 'right' }, { view: 'run', runId: RUN.runId, option: 'default' }, { messages: 30, pooled: true });
    const timeline = q('annotation-timeline');
    const box = () => rect(document.querySelector('[data-annotation-composer-box]')!);
    // The frame over the composer's box: the overlay's element its size, at its top.
    const frames = () => [...document.querySelectorAll<HTMLElement>('[data-pool-overlay] > *')];
    const over = () => frames().find(f => Math.abs(elementY(f) - box().top) < 1.5 && Math.abs(parseFloat(f.style.height) - box().height) < 1.5);
    await expect.poll(() => !!over(), { timeout: 15000 }).toBe(true);
    const frame = over()!;
    await expect.poll(() => frame.style.visibility, { timeout: 15000 }).toBe('visible');
    // At rest: the loop has parked.
    await expect.poll(() => sync().state(), { timeout: 10000 }).toBe('idle');
    const missed = sync().stats().missedWakes;
    const from = timeline.scrollTop;
    // Scrolled up by enough to carry the box's bottom 40px past the log's
    // bottom (how far below the box the composer's controls sit, and so how
    // far the box rests above the log's edge, follows the runner's font
    // metrics: a fixed 80px fell short on GitHub's), the box reaches past
    // the log's bottom: the frame (which nothing in the log clips) follows
    // it there, hidden, so it never paints over what lies below the pane.
    const shift = Math.ceil(rect(timeline).bottom - box().bottom) + 40;
    expect(from, 'the log must overflow by more than the shift').toBeGreaterThan(shift);
    timeline.scrollTop = from - shift;
    expect(box().bottom).toBeGreaterThan(rect(timeline).bottom + 1);
    // Within a few frames, well inside the verify tick (500 ms).
    // (The pass snaps to whole device pixels.)
    await new Promise(resolve => setTimeout(resolve, 120));
    expect(Math.abs(elementY(frame) - box().top)).toBeLessThan(1);
    expect(frame.style.visibility).toBe('hidden');
    // Back in view: shown again, over its box.
    timeline.scrollTop = from;
    await new Promise(resolve => setTimeout(resolve, 120));
    expect(Math.abs(elementY(frame) - box().top)).toBeLessThan(1);
    expect(frame.style.visibility).toBe('visible');
    await new Promise(resolve => setTimeout(resolve, 700));
    expect(sync().stats().missedWakes).toBe(missed);
    expect(Math.abs(elementY(frame) - box().top)).toBeLessThan(1);
});
