// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

const motion = vi.hoisted(() => ({ reduce: true }));
vi.mock('motion/react', async (original) => ({
    ...await original<typeof import('motion/react')>(),
    useReducedMotion: () => motion.reduce,
}));
const persistence = vi.hoisted(() => ({ useOverviewPersistence: vi.fn() }));
vi.mock('@/hooks/useOverviewPersistence', () => persistence);

import { useCollimateStore } from '@/stores/useCollimateStore';
import { REMOVE_RUNNING, SHELF_HEIGHT_DEFAULT, defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { REFUSAL_SHOWN_MS, SHELF_HOVER_OPEN_MS } from '@/hooks/useShelfDrag';
import { RemovedBar } from './RemovedBar';

const s = () => useCollimateStore.getState();
const shelfPx = () => document.querySelector<HTMLElement>('[data-overview-root]')!.style.getPropertyValue('--ov-shelf');
const track = () => screen.getByRole('button', { name: /Removed cells/ });
const bar = () => document.querySelector<HTMLElement>('[data-rm-bar]')!;

function mount(over: Partial<ReturnType<typeof defaultOverviewView>> = {}) {
    useCollimateStore.setState({ overview: { ...defaultOverviewView(), ...over } });
    const view = render(<div data-overview-root><RemovedBar /></div>);
    const root = document.querySelector<HTMLElement>('[data-overview-root]')!;
    Object.defineProperty(root, 'clientHeight', { configurable: true, value: 800 });
    return view;
}

/** A press on the bar's line that moves `dy` and lets go. */
function drag(dy: number, from = 400) {
    const el = bar();
    fireEvent.pointerDown(el, { button: 0, clientY: from, pointerId: 1 });
    fireEvent.pointerMove(el, { clientY: from + dy / 2, pointerId: 1 });
    fireEvent.pointerMove(el, { clientY: from + dy, pointerId: 1 });
    fireEvent.pointerUp(el, { clientY: from + dy, pointerId: 1 });
    fireEvent.click(el);
}

beforeEach(() => {
    vi.clearAllMocks();
    motion.reduce = true;
    useCollimateStore.setState(prev => ({
        selectedCellId: null,
        commandPane: defaultCommandPane(),
        overviewData: { ...prev.overviewData, channel: 'chan', removed: [], removedMatches: null, lineage: null },
    }));
});
afterEach(() => vi.useRealTimers());

describe('RemovedBar', () => {
    it('reads "Removed" and the count, keeps the shelf height in the channel, and sits at the shelf height (O05)', () => {
        mount({ shelfOpen: true, shelfHeight: 280 });
        expect(bar()).toHaveTextContent('Removed0');
        expect(bar().style.top).toBe('var(--ov-shelf, 0px)');
        expect(shelfPx()).toBe('280px');
        expect(persistence.useOverviewPersistence).toHaveBeenCalled();
    });

    it('a click opens and shuts the shelf, and the chevron flips (O05)', () => {
        mount();
        const chevron = document.querySelector('[data-rm-chev]')!;
        expect(track()).toHaveAttribute('aria-expanded', 'false');
        expect(chevron.getAttribute('class')).toContain('rotate-180');
        fireEvent.pointerDown(bar(), { button: 0, clientY: 10, pointerId: 1 });
        fireEvent.pointerUp(bar(), { clientY: 10, pointerId: 1 });
        fireEvent.click(track());
        expect(s().overview.shelfOpen).toBe(true);
        expect(track()).toHaveAttribute('aria-expanded', 'true');
        expect(chevron.getAttribute('class')).toContain('rotate-0');
        expect(shelfPx()).toBe(`${SHELF_HEIGHT_DEFAULT}px`);
        fireEvent.click(chevron);
        expect(s().overview.shelfOpen).toBe(false);
        expect(shelfPx()).toBe('0px');
    });

    it('slides the shelf open over 200ms (O05)', () => {
        motion.reduce = false;
        // Frames are driven by hand, so a loaded machine cannot skip the
        // middle of the slide.
        const frames: FrameRequestCallback[] = [];
        const spies = [
            vi.spyOn(performance, 'now').mockReturnValue(0),
            vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => frames.push(cb)),
            vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {}),
        ];
        onTestFinished(() => spies.forEach(spy => spy.mockRestore()));
        const frame = (t: number) => act(() => { frames.splice(0).forEach(cb => cb(t)); });
        mount();
        expect(shelfPx()).toBe('0px');
        fireEvent.click(track());
        frame(100);
        expect(parseFloat(shelfPx())).toBeGreaterThan(0);
        expect(parseFloat(shelfPx())).toBeLessThan(SHELF_HEIGHT_DEFAULT);
        frame(200);
        expect(shelfPx()).toBe(`${SHELF_HEIGHT_DEFAULT}px`);
        expect(frames).toHaveLength(0);
    });

    it('Enter and Space on the focused bar toggle the shelf (O05)', () => {
        mount();
        track().focus();
        fireEvent.keyDown(track(), { key: 'Enter' });
        expect(s().overview.shelfOpen).toBe(true);
        fireEvent.keyDown(track(), { key: ' ' });
        expect(s().overview.shelfOpen).toBe(false);
    });

    it('a drag resizes the open shelf and keeps the height, without toggling it (O05)', () => {
        mount({ shelfOpen: true, shelfHeight: 300 });
        drag(-80);
        expect(s().overview).toMatchObject({ shelfOpen: true, shelfHeight: 220 });
        expect(shelfPx()).toBe('220px');
    });

    it('a drag that ends below 110px shuts the shelf and keeps the last height (O05)', () => {
        mount({ shelfOpen: true, shelfHeight: 300 });
        drag(-220);
        expect(s().overview).toMatchObject({ shelfOpen: false, shelfHeight: 300 });
        expect(shelfPx()).toBe('0px');
    });

    it('a drag opens a shut shelf to where it is let go, and stops short of the board (O05)', () => {
        mount();
        drag(250);
        expect(s().overview).toMatchObject({ shelfOpen: true, shelfHeight: 250 });
        drag(900);
        expect(s().overview.shelfHeight).toBe(800 - 140);
    });

    it('while the journal is shut its right end offers Journal (O05)', () => {
        mount();
        expect(screen.queryByRole('button', { name: 'Only connected' })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Journal' }));
        expect(s().overview.journalOpen).toBe(true);
        expect(s().overview.shelfOpen).toBe(false);
    });

    it('while the journal is open: Only connected, and Clear while that is on (O05)', () => {
        mount({ journalOpen: true });
        expect(screen.queryByRole('button', { name: 'Journal' })).toBeNull();
        expect(bar().style.right).toBe('374px');
        const toggle = screen.getByRole('button', { name: 'Only connected' });
        expect(toggle).toHaveAttribute('aria-pressed', 'false');
        expect(screen.queryByRole('button', { name: 'Clear' })).toBeNull();
        fireEvent.click(toggle);
        expect(s().overview.onlyConnected).toBe(true);
        expect(toggle).toHaveAttribute('aria-pressed', 'true');
        const clear = screen.getByRole('button', { name: 'Clear' });
        expect(clear).toBeEnabled();
        fireEvent.click(clear);
        expect(s().overview).toMatchObject({ cleared: true, anchor: null });
        expect(clear).toBeDisabled();
        expect(s().overview.shelfOpen).toBe(false);
    });

    it('a wheel over the bar reaches the overview root, which pans the board (O05)', () => {
        mount();
        const root = document.querySelector<HTMLElement>('[data-overview-root]')!;
        const reached = vi.fn();
        root.addEventListener('wheel', reached);
        fireEvent.wheel(bar());
        expect(reached).toHaveBeenCalledTimes(1);
    });

    it('drops the shelf height from the overview root when it goes (O05)', () => {
        const view = mount({ shelfOpen: true });
        const root = document.querySelector<HTMLElement>('[data-overview-root]')!;
        act(() => view.unmount());
        expect(root.style.getPropertyValue('--ov-shelf')).toBe('');
    });
});

describe('RemovedBar as a drop target', () => {
    const hold = (refusal: string | null = null) => act(() => {
        s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'bar', zone: 0, refusal });
    });
    const strip = () => bar().querySelector<HTMLElement>('[data-rm-zone-strip]')!;
    const segments = () => [...strip().querySelectorAll<HTMLElement>('[data-rm-zone]')];
    const lit = () => segments().filter(el => el.hasAttribute('data-lit')).map(el => el.getAttribute('data-rm-zone'));

    it('lights while a board card is held over it, and opens the shut shelf after a 400ms hover (polish)', () => {
        vi.useFakeTimers();
        mount();
        expect(bar()).not.toHaveAttribute('data-drop-target');
        hold();
        expect(bar()).toHaveAttribute('data-drop-target', 'on');
        // One zone: the whole bar is the one plain remove, Standard, lit.
        expect(segments().map(el => el.textContent)).toEqual(['Standard']);
        expect(lit()).toEqual(['0']);
        expect(SHELF_HOVER_OPEN_MS).toBe(400);
        act(() => { vi.advanceTimersByTime(399); });
        expect(s().overview.shelfOpen).toBe(false);
        act(() => { vi.advanceTimersByTime(1); });
        expect(s().overview.shelfOpen).toBe(true);
        act(() => { s().setOverviewDrop(null); });
        expect(bar()).not.toHaveAttribute('data-drop-target');
        expect(bar().className).toContain('bg-canvas');
        expect(segments()).toEqual([]);
    });

    it('a card that leaves the bar sooner, or one held over the shelf, does not open it (polish)', () => {
        vi.useFakeTimers();
        mount();
        hold();
        act(() => { vi.advanceTimersByTime(300); });
        act(() => { s().setOverviewDrop(null); });
        act(() => { vi.advanceTimersByTime(500); });
        expect(s().overview.shelfOpen).toBe(false);
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 0, refusal: null }); });
        expect(bar()).not.toHaveAttribute('data-drop-target');
        act(() => { vi.advanceTimersByTime(500); });
        expect(s().overview.shelfOpen).toBe(false);
    });

    it('refuses a running cell: it does not light or open the shelf, its title says why, and the drop shakes it (polish)', () => {
        vi.useFakeTimers();
        motion.reduce = false;
        const animate = vi.fn();
        Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate });
        onTestFinished(() => { delete (HTMLElement.prototype as { animate?: unknown }).animate; });
        mount();
        hold(REMOVE_RUNNING);
        expect(bar()).toHaveAttribute('data-drop-target', 'refused');
        expect(lit()).toEqual([]);
        expect(bar()).toHaveAttribute('title', REMOVE_RUNNING);
        expect(track()).toHaveAttribute('title', REMOVE_RUNNING);
        act(() => { vi.advanceTimersByTime(SHELF_HOVER_OPEN_MS * 2); });
        expect(s().overview.shelfOpen).toBe(false);
        // Let go: the drag drops its hold and the board refuses the drop on the bar.
        act(() => { s().setOverviewDrop(null); s().refuseDrop(REMOVE_RUNNING, 'bar'); });
        expect(animate).toHaveBeenCalledTimes(1);
        expect(animate.mock.instances[0]).toBe(bar());
        expect(track()).toHaveAttribute('title', REMOVE_RUNNING);
        act(() => { vi.advanceTimersByTime(REFUSAL_SHOWN_MS); });
        expect(track()).toHaveAttribute('title', 'Removed cells · click to open or shut, drag to resize');
        expect(bar()).not.toHaveAttribute('title');
        // A refusal on the shelf is the shelf's to show.
        act(() => { s().refuseDrop(REMOVE_RUNNING, 'shelf'); });
        expect(animate).toHaveBeenCalledTimes(1);
    });
});

describe('RemovedBar split into the remove zones', () => {
    const ZONES = [
        { id: 'z1', width: 0.25, action: { kind: 'add_tag' as const, tag: 'done' } },
        { id: 'z2', width: 0.5, action: { kind: 'remove' as const } },
        { id: 'z3', width: 0.25, action: { kind: 'remove_tag' as const, tag: 'wip' } },
    ];
    const strip = () => bar().querySelector<HTMLElement>('[data-rm-zone-strip]')!;
    const segments = () => [...strip().querySelectorAll<HTMLElement>('[data-rm-zone]')];

    it('at rest shows thin dividers between two or more zones, and none for one zone (TG-06)', () => {
        mount();
        expect(strip()).not.toBeNull();
        expect(strip().querySelectorAll('[data-rm-zone-divider]')).toHaveLength(0);
        act(() => { s().adoptRemoveZones(ZONES); });
        const dividers = [...strip().querySelectorAll<HTMLElement>('[data-rm-zone-divider]')];
        expect(dividers.map(el => el.style.left)).toEqual(['25%', '75%']);
        expect(segments()).toEqual([]);
        expect(strip().className).not.toContain('z-[1]');
        expect(strip().className).toContain('pointer-events-none');
    });

    it('while a removable card is held the bar splits into labelled zones, the one under the card lit teal (TG-06)', () => {
        mount();
        act(() => { s().adoptRemoveZones(ZONES); });
        // Still over the board: the labels show before the card gets here.
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: null, zone: null, refusal: null }); });
        expect(segments().map(el => el.textContent)).toEqual(['+ done', 'Standard', '\u2212 wip']);
        expect(segments().filter(el => el.hasAttribute('data-lit'))).toEqual([]);
        expect(bar()).not.toHaveAttribute('data-drop-target');
        // Held over the shelf: the bar shows the split, nothing lit.
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 2, refusal: null }); });
        expect(segments().map(el => el.textContent)).toEqual(['+ done', 'Standard', '\u2212 wip']);
        expect(segments().map(el => el.style.flex)).toEqual(['0 0 25%', '0 0 50%', '0 0 25%']);
        expect(segments().filter(el => el.hasAttribute('data-lit'))).toEqual([]);
        expect(strip().className).toContain('z-[1]');
        // Over the bar's middle zone: that one lights, with the soft teal on it.
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'bar', zone: 1, refusal: null }); });
        expect(segments().filter(el => el.hasAttribute('data-lit')).map(el => el.textContent)).toEqual(['Standard']);
        expect(segments()[1].className).toContain('text-teal');
        expect(segments()[1].querySelector('.bg-teal-soft')).not.toBeNull();
        expect(segments()[0].querySelector('.bg-teal-soft')).toBeNull();
    });

    it('spans the shelf drop area\'s extent, clear of the Commands pane (TG-06)', () => {
        mount();
        expect(strip().style.left).toBe('0px');
        expect(strip().style.right).toBe('0px');
        act(() => { s().setCommandPaneSide('left'); s().openCommandPane(); });
        expect(strip().style.left).toBe('352px');
        act(() => { s().setCommandPaneSide('right'); });
        expect(strip().style.left).toBe('0px');
        expect(strip().style.right).toBe('352px');
    });
});

describe('RemovedBar under the Commands pane (polish)', () => {
    const dock = (side: 'left' | 'right') => act(() => {
        s().setCommandPaneSide(side);
        s().openCommandPane();
    });
    const under = () => document.querySelector<HTMLElement>('[data-rm-under]')!;

    it('runs the whole width under a pane docked left, its line going on under the pane and its label clear of it (polish)', () => {
        motion.reduce = false;
        mount();
        expect(under().style.width).toBe('0px');
        dock('left');
        // The ground is unchanged: the bar still spans the overview.
        expect(bar().className).toContain('left-0');
        expect(bar().style.right).toBe('0px');
        expect(bar().style.marginRight).toBe('0px');
        expect(bar().style.paddingRight).toBe('10px');
        // The line runs on under the pane, as wide as it, before the
        // lead and the label: the label sits as far past the pane's
        // edge as it sits past the overview's without it.
        expect(under().style.width).toBe('352px');
        expect(under().className).toContain('bg-line-2');
        expect(track().firstElementChild).toBe(under());
        expect(under().style.transition).toBe('background-color 120ms ease-out, width 160ms ease');
    });

    it('keeps its right-end Journal toggle clear of a pane docked right, the ground running on under it (polish)', () => {
        motion.reduce = false;
        mount();
        dock('right');
        expect(bar().style.right).toBe('0px');
        expect(bar().style.marginRight).toBe('0px');
        expect(bar().style.paddingRight).toBe('362px');
        expect(under().style.width).toBe('0px');
        expect(bar().style.transition).toBe('padding-right 160ms ease, margin-right 160ms ease');
        expect(bar().querySelector('[data-rm-journal]')).not.toBeNull();
    });

    it('ends at a journal docked against the pane, its controls in their usual place (polish)', () => {
        mount({ journalOpen: true });
        dock('right');
        expect(bar().style.right).toBe('374px');
        expect(bar().style.marginRight).toBe('352px');
        expect(bar().style.paddingRight).toBe('10px');
    });

    it('jumps clear with reduced motion (polish)', () => {
        mount();
        dock('right');
        expect(bar().style.paddingRight).toBe('362px');
        expect(bar().style.transition).toBe('');
        expect(under().style.transition).toBe('');
    });
});
