// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import type { CSSProperties } from 'react';
import type { Channel, Stack } from '@/lib/types';
import type { Lineage, RemovedCell } from '@/lib/api/cells';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { OverviewArrows } from './OverviewArrows';

// The measured overview: every part carries `data-rect="left,top,width,height"`
// (overview-local, the root at 0,0); a label is 6px a character. A card the
// camera moves carries its place in its style too, as the camera's transform
// does, so the layer sees the move.
function rectOf(el: Element): DOMRect {
    const spec = el.getAttribute('data-rect');
    if (spec) {
        const [l, t, w, h] = spec.split(',').map(Number);
        return { left: l, top: t, right: l + w, bottom: t + h, width: w, height: h, x: l, y: t, toJSON: () => ({}) };
    }
    const w = el.parentElement?.hasAttribute('data-ov-labels') ? (el.textContent ?? '').length * 6 : 0;
    return { left: 0, top: 0, right: w, bottom: 18, width: w, height: 18, x: 0, y: 0, toJSON: () => ({}) };
}

const stack = (id: string, ids: [string, string][]): Stack => ({
    id, name: id, cells: ids.map(([cid, name]) => ({ id: cid, name, files: [], created_at: '2026-10-01T09:00:00' })),
});
const channel: Channel = {
    id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1',
    main_stacks: [stack('s1', [['a', 'Frame'], ['b', 'LWW']]), stack('s2', [['c', 'Op log']]), stack('s3', [['m', 'Hybrid']])],
};
const removedD = { cell_id: 'd', name: 'Clocks' } as RemovedCell;
// a was duplicated as d (on the shelf), merged with c into m, and split into x
// (a cell the overview does not show).
const end = (cell_id: string) => ({ cell_id, version_id: null });
const lineage: Lineage = {
    cells: [{ cell_id: 'x', name: 'Elsewhere', status: 'removed' }],
    moves: [
        { id: 1, op: 'duplicate', at: '2026-10-04T10:00:00+00:00', op_id: 'o1', from: [end('a')], to: [end('d')] },
        { id: 2, op: 'merge', at: '2026-10-04T11:00:00+00:00', op_id: 'o2', from: [end('a'), end('c')], to: [end('m')] },
        { id: 3, op: 'split', at: '2026-10-04T12:00:00+00:00', op_id: 'o3', from: [end('a')], to: [end('x')] },
        { id: 4, op: 'run_cell', at: '2026-10-04T13:00:00+00:00', op_id: 'o4', from: [end('a')], to: [end('b')] },
    ],
};

/** The overview at a shelf height: the bar under the shelf, three columns. */
function Scene({ shelfPx, aTop = 60 }: { shelfPx: number; aTop?: number }) {
    const y = (top: number) => top + shelfPx;
    return (
        <div data-overview-root data-rect="0,0,1000,800" style={{ '--ov-shelf': `${shelfPx}px` } as CSSProperties}>
            {shelfPx > 0 && (
                <div data-rm-grid data-rect={`0,36,660,${shelfPx - 36}`}>
                    <article data-rm-card="d" data-rect="18,50,236,118" />
                </div>
            )}
            <div data-rm-bar data-rect={`0,${shelfPx},660,30`}>
                <span data-rm-n data-rect={`70,${y(6)},20,18`} />
                <button type="button" data-rect={`540,${y(3)},100,24`}>Only connected</button>
                <svg data-rm-chev data-rect={`642,${y(9)},12,12`} />
            </div>
            <div data-ov-column>
                <article data-ov-card="a" data-rect={`18,${y(aTop)},272,248`} style={{ top: aTop }} />
                <article data-ov-card="b" data-rect={`18,${y(326)},272,248`} />
            </div>
            <div data-ov-column><article data-ov-card="c" data-rect={`308,${y(60)},272,248`} /></div>
            <div data-ov-column><article data-ov-card="m" data-rect={`598,${y(60)},40,248`} /></div>
            <OverviewArrows />
        </div>
    );
}

const s = () => useCollimateStore.getState();
const arrows = () => [...document.querySelectorAll('[data-ov-arrows] g')];
const labels = () => [...document.querySelectorAll<HTMLElement>('[data-ov-lab]')].map(l => [l.getAttribute('data-ov-lab'), l.textContent]);
const label = (id: string) => document.querySelector<HTMLElement>(`[data-ov-lab="${id}"]`)!;

beforeEach(() => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) { return rectOf(this); });
    useCollimateStore.setState({
        activeChannel: channel, selectedCellId: null, runs: {}, pendingSelectCellId: null, commandPane: defaultCommandPane(),
    });
    s().dropOverviewData();
    useCollimateStore.setState(state => ({
        overview: { ...defaultOverviewView(), journalOpen: true },
        overviewData: { ...state.overviewData, channel: 'chan', removed: [removedD], lineage },
    }));
    useCollimateStore.setState({ selectedCellId: 'a' });
});
afterEach(() => {
    vi.restoreAllMocks();
    useCollimateStore.setState({ commandPane: defaultCommandPane() });
});

describe('the lineage arrows', () => {
    it('draw one line per duplicate, merge and split of the subject, none for a run in a new cell (O07)', () => {
        render(<Scene shelfPx={0} />);
        // m is on the board: a line and a mid-line label; d (the shelf is
        // shut) and x (not shown) are met on the bar, named there.
        expect(arrows().map(g => g.getAttribute('data-arrow'))).toEqual(['solid', 'dashed', 'solid']);
        expect(labels()).toEqual([['m', 'merge'], ['d', 'duplicate Clocks'], ['x', 'split Elsewhere']]);
        const paths = arrows()[1].querySelectorAll('path');
        expect(paths[0].style.strokeDasharray).toBe('4 3');
        expect(paths[0].style.stroke).toBe('var(--ink-2)');
        expect(paths[0].style.strokeWidth).toBe('1.25');
        // The layer never takes the pointer.
        expect(document.querySelector('[data-ov-arrows]')!.getAttribute('class')).toMatch(/pointer-events-none/);
    });

    it('rows the bar labels right of the Removed count, short of the bar\'s controls (O07)', () => {
        render(<Scene shelfPx={0} />);
        const left = (id: string) => Number.parseFloat(label(id).style.left);
        expect(left('d')).toBeGreaterThanOrEqual(90 + 16);
        expect(left('x')).toBe(left('d') + 'duplicate Clocks'.length * 6 + 14);
        expect(left('x') + 'split Elsewhere'.length * 6).toBeLessThanOrEqual(540 - 18);
        expect(label('d').style.top).toBe('7.5px');
    });

    it('meets a shelf card at its foot once the shelf shows it, and follows the shelf\'s height (O07)', async () => {
        const view = render(<Scene shelfPx={0} />);
        view.rerender(<Scene shelfPx={316} />);
        await waitFor(() => expect(labels()).toEqual([['d', 'duplicate'], ['m', 'merge'], ['x', 'split Elsewhere']]));
        // The duplicate's line leaves a (now below the shelf) and ends on d's foot.
        const dup = arrows().find(g => g.getAttribute('data-arrow') === 'dashed')!;
        expect(dup.querySelectorAll('path')[1].getAttribute('d')).toMatch(/^M\d+(\.\d+)? 168L/);
        expect(label('x').style.top).toBe(`${316 + 7.5}px`);
    });

    it('draws the line to the selected cell hot with Only connected on (O07)', async () => {
        render(<Scene shelfPx={0} />);
        expect(document.querySelector('[data-ov-arrows] g[data-hot]')).toBeNull();
        act(() => { s().toggleConnected(); useCollimateStore.setState({ selectedCellId: 'm' }); });
        await waitFor(() => expect(document.querySelectorAll('[data-ov-arrows] g[data-hot]')).toHaveLength(1));
        const hot = document.querySelector('[data-ov-arrows] g[data-hot] path') as SVGPathElement;
        expect(hot.style.stroke).toBe('var(--ink)');
        expect(hot.style.strokeWidth).toBe('1.75');
    });

    it('walks from a label: to a board cell, or into the opened shelf (O07)', () => {
        render(<Scene shelfPx={0} />);
        expect(label('m')).toHaveAccessibleName('merge · Hybrid');
        fireEvent.click(label('m'));
        expect(s().pendingSelectCellId).toBe('m');
        fireEvent.click(label('d'));
        expect(s().overview).toMatchObject({ shelfOpen: true, picked: 'd', scrollTo: 'd' });
    });

    it('names a cell neither on the board nor on the shelf without walking to it (O07)', () => {
        render(<Scene shelfPx={0} />);
        // x is recorded in the lineage but shown nowhere: a plain label.
        const x = label('x');
        expect(x.tagName).toBe('SPAN');
        expect(x.title).toBe('split · Elsewhere');
        expect(x.className).not.toMatch(/cursor-pointer|hover:/);
        const before = s().overview;
        fireEvent.click(x);
        expect(s().overview).toBe(before);
        expect(s().pendingSelectCellId).toBeNull();
        expect(s().selectedCellId).toBe('a');
        // The cells it can walk to stay buttons.
        expect(label('m').tagName).toBe('BUTTON');
        expect(label('d').tagName).toBe('BUTTON');
    });

    it('draws nothing while the journal is shut, or for a subject out of view (O07)', async () => {
        const view = render(<Scene shelfPx={0} />);
        act(() => s().setJournalOpen(false));
        expect(document.querySelector('[data-ov-arrows]')).toBeNull();
        act(() => s().setJournalOpen(true));
        expect(arrows()).toHaveLength(3);
        // a (three moves) panned below the board's view.
        view.rerender(<Scene shelfPx={0} aTop={900} />);
        await waitFor(() => expect(arrows()).toEqual([]));
        expect(labels()).toEqual([]);
        // a in view again, then under the opened shelf.
        view.rerender(<Scene shelfPx={0} />);
        await waitFor(() => expect(arrows()).toHaveLength(3));
        view.rerender(<Scene shelfPx={316} aTop={-300} />);
        await waitFor(() => expect(arrows()).toEqual([]));
        expect(labels()).toEqual([]);
    });

    it('leave out what lies under the Commands pane, and redraw as it docks and shuts (polish)', async () => {
        render(<Scene shelfPx={0} />);
        expect(arrows()).toHaveLength(3);
        // Docked left, the Commands pane covers a (18..290): the subject is
        // out of view, so nothing is drawn.
        act(() => { s().setCommandPaneSide('left'); s().openCommandPane(); });
        await waitFor(() => expect(arrows()).toEqual([]));
        expect(labels()).toEqual([]);
        act(() => s().closeCommandPane());
        await waitFor(() => expect(arrows()).toHaveLength(3));
        expect(labels()).toEqual([['m', 'merge'], ['d', 'duplicate Clocks'], ['x', 'split Elsewhere']]);
        // Docked right, the board ends before the journal and Commands pane at the overview's
        // right edge: m (from 598) is off the board, met on the bar and named
        // there.
        act(() => { s().setCommandPaneSide('right'); s().openCommandPane(); });
        await waitFor(() => expect(labels()).toContainEqual(['m', 'merge Hybrid']));
        expect(Number.parseFloat(label('m').style.top)).toBe(7.5);
        // The board's slide out from under the pane ends in CSS, which no
        // observer sees: its transitionend redraws (a measured, out of view).
        document.querySelector('[data-ov-card="a"]')!.setAttribute('data-rect', '18,900,272,248');
        fireEvent.transitionEnd(document.querySelector('[data-ov-card="a"]')!);
        await waitFor(() => expect(arrows()).toEqual([]));
    });
});
