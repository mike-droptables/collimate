// The overview under the Commands pane, in Chromium: the real camera and
// layout under StackScene, the real Removed bar, shelf and journal. The pane
// itself lives in StackApp; its state is the store's.
import { render } from 'vitest-browser-react';
import { act } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import type { Stack } from '@/lib/types';
import type { SceneActions } from '@/hooks/useSceneActions';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { COMMAND_PANE_WIDTH, defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { JOURNAL_PANE_WIDTH, OVERVIEW_CARD_HEIGHT, defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { px } from '@/test/rootFontSize';
import { shelfZoneAt } from '@/hooks/useShelfDrag';
import { maxStackWidthFor } from '@/hooks/useStackLayout';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { FocusProvider } from '@/contexts/FocusContext';
import { StackScene } from './StackScene';

vi.mock('@/lib/api/cells', async (original) => ({
    ...await original<typeof import('@/lib/api/cells')>(),
    fetchJournal: vi.fn(async () => []), fetchLineage: vi.fn(async () => ({ cells: [], moves: [] })),
    fetchRemovedCells: vi.fn(async () => []),
}));
vi.mock('@/hooks/useOverviewPersistence', () => ({ useOverviewPersistence: () => {} }));
vi.mock('@/contexts/GuestPoolContext', () => ({
    useGuestPool: () => ({
        registerOverlay: () => {}, bringCellsToFront: () => {}, invalidate: () => {},
        markMoving: () => {}, registerMotionLayer: () => () => {},
    }),
}));
vi.mock('@/hooks/useEntryHold', () => ({ useEntryHold: () => new Set() }));
vi.mock('./SceneDragOverlays', () => ({ SceneDragOverlays: () => null, SceneCursorOverlays: () => null }));
// A column: its cards, a card's height, as the overview lays them out, and the
// width the layout gives it.
vi.mock('./StackGroup', () => ({
    StackGroup: ({ stack, width }: { stack: Stack; width: number }) => (
        <div data-ov-column data-stack={stack.id} data-width={width}>
            {stack.cells.map(c => <div key={c.id} data-ov-card={c.id} style={{ height: OVERVIEW_CARD_HEIGHT }}>{c.name}</div>)}
        </div>
    ),
    NO_OP_STACK_CALLBACKS: {}, FLOATING_PREVIEW_STACK_ID: 'floating',
}));

const stacks: Stack[] = ['a', 'b', 'c', 'd', 'e'].map(id => ({ id: `s-${id}`, name: id, cells: [{ id, name: id.toUpperCase(), files: [] }] }));
const s = () => useCollimateStore.getState();
const rect = (el: Element) => el.getBoundingClientRect();
const q = (sel: string) => document.querySelector<HTMLElement>(sel)!;
const dock = (side: 'left' | 'right' | null) => act(() => {
    if (!side) { s().closeCommandPane(); return; }
    s().setCommandPaneSide(side);
    s().openCommandPane();
});

const prior = useCollimateStore.getState();
const priorWidths = useSpatialStore.getState().stackWidths;
beforeEach(() => {
    useCollimateStore.setState({
        activeChannel: { id: 'ch', name: 'ch', main_stacks: stacks, nav_stacks: [] } as never,
        selectedCellId: null, commandPane: defaultCommandPane(),
        overview: defaultOverviewView(),
        overviewData: { ...prior.overviewData, channel: 'ch', removed: [], removedMatches: null, lineage: { cells: [], moves: [] } },
    });
});
afterEach(() => {
    useSpatialStore.setState({ stackWidths: priorWidths });
    useCollimateStore.setState({
        commandPane: defaultCommandPane(), overview: defaultOverviewView(), overviewData: prior.overviewData,
    });
});

const scene = (isZoomedOut: boolean) => (
    <FocusProvider><div data-testid="canvas" style={{ position: 'relative', width: 1200, height: 800, overflow: 'hidden' }}>
        <StackScene stacks={stacks} selectedId="s-a" actions={{} as SceneActions} transitionType="nav"
            topology="stable" isZoomedOut={isZoomedOut} fan={null} fanAnchor={null} onCollapseFan={() => {}}
            onSelectStack={() => {}} onZoomToCell={() => {}} />
    </div></FocusProvider>
);

async function mount(isZoomedOut = true) {
    await page.viewport(1400, 900);
    const view = await render(scene(isZoomedOut));
    const root = q('[data-overview-root]');
    // The entry fit settles: the cards stop moving.
    let last = '';
    await expect.poll(() => {
        const now = JSON.stringify(rect(q('[data-ov-card="a"]')));
        const settled = now === last;
        last = now;
        return settled;
    }, { interval: 100, timeout: 4000 }).toBe(true);
    return { view, root, board: q('[data-overview-board]') };
}

// The cards' screen geometry: a column is 272px wide in the world, so a
// card's width on screen is 272 times the zoom.
const card = (id: string) => rect(q(`[data-ov-card="${id}"]`));
const zoomOnScreen = () => card('a').width / 272;
/** Samples a value every frame until stopped. */
function sampler(read: () => number) {
    const seen: number[] = [];
    let on = true;
    const tick = () => {
        if (!on) return;
        seen.push(read());
        requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    return { seen, stop: () => { on = false; } };
}
/** Settles once a value stops changing. */
async function settled(read: () => number) {
    let last = NaN;
    await expect.poll(() => {
        const now = read();
        const still = Math.abs(now - last) < 0.01;
        last = now;
        return still;
    }, { interval: 100, timeout: 4000 }).toBe(true);
}
/** Lets a 160ms slide finish and a frame or two past it. */
const slid = () => new Promise(resolve => setTimeout(resolve, 300));
const unpainted = (el: Element) => {
    const cs = getComputedStyle(el);
    expect(cs.backgroundColor).toBe('rgba(0, 0, 0, 0)');
    expect(cs.backgroundImage).toBe('none');
    expect(cs.boxShadow).toBe('none');
    for (const side of ['Top', 'Right', 'Bottom', 'Left'] as const) expect(cs[`border${side}Width`]).toBe('0px');
};

it('a pane docked right leaves the board\'s content and zoom where they were, every frame, while the right-anchored controls move in and the ground runs on under it (polish)', async () => {
    const { view, root, board } = await mount();
    try {
        const r = rect(root);
        const inner = r.right - COMMAND_PANE_WIDTH;
        const a0 = card('a').left;
        const e0 = card('e').left;
        const z0 = zoomOnScreen();
        const xs = sampler(() => card('a').left);
        dock('right');
        await expect.poll(() => rect(board).right).toBe(inner);
        await slid();
        xs.stop();
        expect(xs.seen.length).toBeGreaterThan(5);
        xs.seen.forEach(x => expect(x).toBeCloseTo(a0, 0));
        expect(zoomOnScreen()).toBeCloseTo(z0, 4);
        expect(card('e').left).toBeCloseTo(e0, 0);
        // The board region ends at the pane with no edge or ground of its own.
        expect(rect(board).left).toBe(r.left);
        unpainted(board);
        unpainted(root);
        // The right-anchored controls move in by the pane; the bar's ground runs on under it.
        await expect.poll(() => rect(q('[data-overview-zoom]')).right).toBe(inner - 15);
        await expect.poll(() => rect(q('[data-rm-chev]')).right).toBe(inner - 10);
        expect(rect(q('[data-rm-journal]')).right).toBeLessThanOrEqual(inner);
        const bar = q('[data-rm-bar]');
        expect(rect(bar).right).toBe(r.right);
        // The journal docks against the pane, the board and the bar ending at it.
        act(() => s().setJournalOpen(true));
        const journal = q('[data-journal-pane]');
        await expect.poll(() => rect(journal).right).toBe(inner);
        expect(rect(journal).width).toBe(JOURNAL_PANE_WIDTH);
        await expect.poll(() => rect(board).right).toBe(inner - JOURNAL_PANE_WIDTH);
        await expect.poll(() => rect(bar).right).toBe(inner - JOURNAL_PANE_WIDTH);
        await expect.poll(() => rect(q('[data-overview-zoom]')).right).toBe(inner - JOURNAL_PANE_WIDTH - 15);
        await slid();
        expect(card('a').left).toBeCloseTo(a0, 0);
        expect(zoomOnScreen()).toBeCloseTo(z0, 4);
        // The pane goes: still nothing moves on the board.
        dock(null);
        await expect.poll(() => rect(journal).right).toBe(r.right);
        await slid();
        expect(card('a').left).toBeCloseTo(a0, 0);
        expect(zoomOnScreen()).toBeCloseTo(z0, 4);
    } finally {
        await view.unmount();
    }
});

it('a pane docked left slides the board\'s content right by its width at the same zoom, the right-anchored controls staying put; docked across and closed, it slides back (polish)', async () => {
    const { view, root, board } = await mount();
    try {
        const r = rect(root);
        act(() => s().setJournalOpen(true));
        const journal = q('[data-journal-pane]');
        await expect.poll(() => rect(journal).right).toBe(r.right);
        await expect.poll(() => rect(board).right).toBe(r.right - JOURNAL_PANE_WIDTH);
        await slid();
        const anchored = () => ['[data-journal-pane]', '[data-rm-connected]', '[data-overview-zoom]', '[data-rm-chev]']
            .map(sel => rect(q(sel)).right);
        const before = anchored();
        const z0 = zoomOnScreen();
        const a0 = card('a').left;
        const e0 = card('e').left;
        const label = () => rect(document.querySelector('[data-rm-bar] [data-rm-track] span:nth-child(3)')!).left;
        const labelX = label();
        // The content's place against the region's left edge, every frame.
        const xs = sampler(() => card('a').left - rect(board).left);
        dock('left');
        // A 160ms slide, not a jump.
        expect(getComputedStyle(board).transitionDuration).toBe('0.16s, 0.16s');
        await expect.poll(() => rect(board).left).toBe(r.left + COMMAND_PANE_WIDTH);
        await slid();
        xs.stop();
        // Every frame the content rides the region's sliding left edge.
        expect(xs.seen.length).toBeGreaterThan(5);
        xs.seen.forEach(x => expect(x).toBeCloseTo(a0 - r.left, 0));
        expect(card('a').left).toBeCloseTo(a0 + COMMAND_PANE_WIDTH, 0);
        expect(card('e').left).toBeCloseTo(e0 + COMMAND_PANE_WIDTH, 0);
        expect(zoomOnScreen()).toBeCloseTo(z0, 4);
        expect(rect(board).right).toBe(r.right - JOURNAL_PANE_WIDTH);
        unpainted(board);
        // Nothing anchored right moved.
        expect(anchored()).toEqual(before);
        // The bar's ground spans the whole overview's left; its line runs on
        // under the pane, and its label sits past the pane's edge as far as
        // it sat past the overview's.
        const bar = q('[data-rm-bar]');
        expect(rect(bar).left).toBe(r.left);
        await expect.poll(() => rect(q('[data-rm-under]')).width).toBe(COMMAND_PANE_WIDTH);
        // The bar's 1.125rem left padding.
        expect(rect(q('[data-rm-under]')).left).toBeCloseTo(r.left + px(1.125), 1);
        await expect.poll(() => label()).toBe(labelX + COMMAND_PANE_WIDTH);
        // Docked across: the region's left edge is the overview's again.
        dock('right');
        await expect.poll(() => rect(board).left).toBe(r.left);
        await slid();
        expect(card('a').left).toBeCloseTo(a0, 0);
        expect(zoomOnScreen()).toBeCloseTo(z0, 4);
        dock('left');
        await expect.poll(() => rect(board).left).toBe(r.left + COMMAND_PANE_WIDTH);
        await slid();
        dock(null);
        await expect.poll(() => rect(board).left).toBe(r.left);
        await slid();
        expect(card('a').left).toBeCloseTo(a0, 0);
        expect(zoomOnScreen()).toBeCloseTo(z0, 4);
        expect(anchored()).toEqual(before);
    } finally {
        await view.unmount();
    }
});

it('the board cuts no card off at its sides: a card past the region\'s edge runs on under the pane to the overview\'s edge, so a docking-across slide bares no clipped edge (polish)', async () => {
    const { view, root, board } = await mount();
    try {
        const r = rect(root);
        dock('right');
        await expect.poll(() => rect(board).right).toBe(r.right - COMMAND_PANE_WIDTH);
        await slid();
        const e = card('e');
        // The rightmost card straddles or passes the region's edge.
        expect(e.right).toBeGreaterThan(rect(board).right);
        const x = Math.max(e.left, rect(board).right) + 4;
        expect(x).toBeLessThan(Math.min(e.right, r.right));
        const hit = document.elementFromPoint(x, e.top + e.height / 2);
        expect(hit?.closest('[data-ov-card]')?.getAttribute('data-ov-card')).toBe('e');
        // Above and below it still ends: the bar and the shelf keep their place.
        expect(getComputedStyle(board).overflowY).toBe('clip');
        unpainted(board);
    } finally {
        await view.unmount();
    }
});

it('entering the overview with the pane open fits the board to the region it leaves, on either side (polish)', async () => {
    await page.viewport(1400, 900);
    for (const side of ['left', 'right'] as const) {
        dock(side);
        const view = await render(scene(false));
        try {
            await slid();
            await view.rerender(scene(true));
            const board = q('[data-overview-board]');
            await expect.poll(() => rect(board).width).toBe(1200 - COMMAND_PANE_WIDTH);
            await settled(() => card('e').right - card('a').left);
            await settled(() => card('a').left);
            // Five 272px columns 18px apart, the world padded 18px either side.
            const z = (1200 - COMMAND_PANE_WIDTH) / (5 * 272 + 6 * 18);
            expect(zoomOnScreen()).toBeCloseTo(z, 3);
            expect(card('a').left - rect(board).left).toBeCloseTo(18 * z, 0);
            expect(rect(board).right - card('e').right).toBeCloseTo(18 * z, 0);
        } finally {
            await view.unmount();
        }
    }
});

it('a card dragged over the bar meets it only past the pane, against the inset board (polish)', async () => {
    const { view, root, board } = await mount();
    try {
        dock('left');
        const r = rect(root);
        await expect.poll(() => rect(board).left).toBe(r.left + COMMAND_PANE_WIDTH);
        const barY = rect(q('[data-rm-bar]')).top + 15;
        // Under the pane the bar is not a drop target; past it, it is.
        expect(shelfZoneAt(r.left + 100, barY, rect(board), r)).toBeNull();
        expect(shelfZoneAt(r.left + COMMAND_PANE_WIDTH + 40, barY, rect(board), r)).toBe('bar');
    } finally {
        await view.unmount();
    }
});

it('in the main view a full-width stack keeps inside what the open pane leaves free; the overview\'s columns are not inset twice (polish)', async () => {
    const width = () => Number(q('[data-ov-column][data-stack="s-a"]').dataset.width);
    act(() => useSpatialStore.setState({ stackWidths: { 's-a': maxStackWidthFor(1200) } }));
    const main = await mount(false);
    try {
        await expect.poll(width).toBe(maxStackWidthFor(1200));
        // Its edges on screen, from the canvas's left.
        const edges = () => {
            const col = rect(q('[data-ov-column][data-stack="s-a"]').parentElement!);
            const left = rect(q('[data-testid="canvas"]')).left;
            return [Math.round(col.left - left), Math.round(col.right - left)];
        };
        await slid();
        const [l0, r0] = edges();
        // Docked right, its left edge stays and its right edge moves in.
        dock('right');
        await expect.poll(width).toBe(maxStackWidthFor(1200 - COMMAND_PANE_WIDTH));
        await expect.poll(edges, { timeout: 4000 }).toEqual([l0, r0 - COMMAND_PANE_WIDTH]);
        // Docked left, it slides right: its left edge by the pane, its right edge stays.
        dock('left');
        await expect.poll(edges, { timeout: 4000 }).toEqual([l0 + COMMAND_PANE_WIDTH, r0]);
        dock(null);
        await expect.poll(width).toBe(maxStackWidthFor(1200));
        await expect.poll(edges, { timeout: 4000 }).toEqual([l0, r0]);
    } finally {
        await main.view.unmount();
    }
    // Zoomed out the board itself is inset (boardStyle): the layout keeps
    // its columns at their width under an open pane.
    const over = await mount(true);
    try {
        const shut = width();
        dock('left');
        await expect.poll(() => rect(over.board).left).toBe(rect(over.root).left + COMMAND_PANE_WIDTH);
        expect(width()).toBe(shut);
    } finally {
        await over.view.unmount();
    }
});
