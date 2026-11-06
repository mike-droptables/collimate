import { fileFixture, fixtureText } from '@/test/fileFixtures';
/**
 * The status pane's drilldown and its composer in a real cell, in Chromium
 * with real CSS, docked on the right (280px) and across the bottom (200px):
 * the view's rail on the icon line, the options under it, the timeline
 * filling the space down to the composer, whose controls row ends at the
 * pane's bottom; nothing overflows the pane; Expand gives the composer the
 * pane; ten messages scroll inside the timeline only; the status view's
 * rows and footer fit a 200px column. The composer's editor is the native
 * probe renderer. Run with VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { createDefaultLayout, setRunsPane } from '@/lib/cellLayout';
import { FIXTURE_PROBE_ID, type ProbeState } from '../test/rendererFixtures';
import type { Cell, Stack } from '@/lib/types';
import type { RunsPaneState, RunsPaneView } from '@/stores/collimate/types';
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
// Every file, the composer's draft included, opens in the native probe.
vi.mock('@/hooks/useRendererId', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    useRendererId: () => ({renderer:{
        id: FIXTURE_PROBE_ID, name: 'Probe', extensions: ['.md'], supportsDiff: false,
        liveRefresh: false, embed: 'native', contentHash: 'composer-probe',
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

async function mount(runs: Partial<RunsPaneState>, view: RunsPaneView, messages = 2) {
    await page.viewport(1000, 700);
    (window as unknown as { __probe: ProbeState }).__probe = { mounts: 0, cleanups: 0, apis: [], fileUrlsAtMount: [], vis: [], set: [] };
    useCollimateStore.setState({
        cellLayouts: { 'cell-1': seed(runs) },
        state: { active_channel_id: 'ch', channels: [{ id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [], main_selection_id: null, nav_selection_id: null }] },
        cellCache: { [cell.id]: cell }, runs: { [RUN.runId]: RUN }, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        cellDiffs: {}, runPhases: {}, lastFinishedByCell: {}, activeRunsEverywhere: [], cellLogRuns: {}, cellDisplaysReady: true,
        maximizedOverlayCellId: null, dockDrag: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [] } as never,
        runsPaneView: { 'cell-1': view }, finishedRuns: {}, composerDrafts: {}, fileTouches: { [RUN.runId]: [] },
        annotationContexts: { 'cell-1': { [RUN.runId]: context(messages) } },
        loadCellAnnotations: async () => {}, refreshAnnotationContext: async () => {}, loadFileTouches: async () => {},
    } as never);
    await render(
        <FocusProvider>
            <div style={{ position: 'relative', width: 800, height: 500, display: 'flex', flexDirection: 'column' }}>
                <CellEditor cell={cell} onUpdate={() => {}} />
            </div>
        </FocusProvider>,
    );
    await expect.poll(() => document.querySelector('aside[aria-label="Status"]')).not.toBeNull();
    const pane = document.querySelector<HTMLElement>('aside[aria-label="Status"]')!;
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
] as const)('docked %s, the drilldown stacks rail, options, timeline and composer down the pane (%dpx)', async (side, size) => {
    const { pane } = await mount({ open: true, side }, { view: 'run', runId: RUN.runId, option: 'default' });
    await expect.poll(() => document.querySelector('[data-testid="annotation-composer"]')).not.toBeNull();
    await expect.poll(() => probe().mounts, { timeout: 15000 }).toBeGreaterThanOrEqual(2);
    const p = rect(pane);
    expect(side === 'right' ? p.width : p.height).toBeCloseTo(size, 0);
    const hide = rect(document.querySelector('button[aria-label="Hide status"]')!);
    const back = rect(q('drilldown-back'));
    expect(back.bottom).toBeCloseTo(hide.bottom, 0);
    const rail = q('drilldown-back').closest('.h-\\[1\\.875rem\\]')!;
    const options = q('drilldown-option-default').parentElement!;
    expect(rect(options).top).toBeCloseTo(rect(rail).bottom, 0);
    const timeline = rect(q('annotation-timeline'));
    const composer = rect(q('annotation-composer'));
    expect(timeline.top).toBeGreaterThanOrEqual(rect(options).bottom - 0.5);
    expect(timeline.bottom).toBeCloseTo(composer.top, 0);
    expect(composer.bottom).toBeCloseTo(p.bottom, 0);
    const box = rect(document.querySelector('[data-annotation-composer-box]')!);
    const controls = rect(q('composer-send').parentElement!);
    expect(controls.bottom).toBeCloseTo(p.bottom, 0);
    expect(box.bottom).toBeCloseTo(controls.top, 0);
    expect(box.height).toBeGreaterThan(40);
    fitsAcross(pane);
});

it('Expand gives the composer the pane, the timeline hidden; Collapse puts it back', async () => {
    const { pane } = await mount({ open: true, side: 'right' }, { view: 'run', runId: RUN.runId, option: 'default' });
    await expect.poll(() => document.querySelector('[data-testid="composer-expand"]')).not.toBeNull();
    const before = rect(document.querySelector('[data-annotation-composer-box]')!).height;
    q('composer-expand').click();
    await expect.poll(() => q('annotation-composer').getAttribute('data-expanded')).toBe('true');
    expect(getComputedStyle(q('annotation-timeline').closest('[role="tabpanel"]')!).display).toBe('none');
    const expanded = rect(document.querySelector('[data-annotation-composer-box]')!);
    expect(expanded.height).toBeGreaterThan(before + 150);
    expect(rect(q('annotation-composer')).bottom).toBeCloseTo(rect(pane).bottom, 0);
    q('composer-expand').click();
    await expect.poll(() => q('annotation-composer').getAttribute('data-expanded')).toBe('false');
    expect(rect(document.querySelector('[data-annotation-composer-box]')!).height).toBeCloseTo(before, 0);
});

it('ten messages scroll inside the timeline alone; the composer stays where it is', async () => {
    const { pane } = await mount({ open: true, side: 'right' }, { view: 'run', runId: RUN.runId, option: 'default' }, 10);
    await expect.poll(() => document.querySelectorAll('[data-testid="annotation-message"]').length).toBe(10);
    const timeline = q('annotation-timeline');
    expect(timeline.scrollHeight).toBeGreaterThan(timeline.clientHeight);
    const composerTop = rect(q('annotation-composer')).top;
    expect(rect(q('annotation-composer')).bottom).toBeCloseTo(rect(pane).bottom, 0);
    timeline.scrollTop = 0;
    await expect.poll(() => timeline.scrollTop).toBe(0);
    expect(rect(q('annotation-composer')).top).toBeCloseTo(composerTop, 0);
    expect(pane.scrollHeight).toBeLessThanOrEqual(pane.clientHeight + 1);
});

it('the status view\'s rows and footer fit a 200px column', async () => {
    useCollimateStore.setState({ finishedRuns: {} });
    const { pane } = await mount({ open: true, side: 'right', width: 200 }, { view: 'status' });
    await expect.poll(() => document.querySelector('[data-testid="status-run-row"]')).not.toBeNull();
    expect(rect(pane).width).toBeCloseTo(200, 0);
    for (const id of ['status-run-row', 'status-all', 'status-new-run']) {
        expect(rect(q(id)).right).toBeLessThanOrEqual(rect(pane).right + 0.5);
    }
    fitsAcross(pane);
});
