import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type React from 'react';
import { PAN_BUTTONS, PINCH_ZOOM_PER_PX, WHEEL_ZOOM_PER_PX, usePanZoomCamera, zoomCameraAt } from '../usePanZoomCamera';
import { OVERVIEW_ZOOM, OVERVIEW_ZOOM_STEP } from '@/stores/collimate/overviewSlice';
import { WHEEL_LINE_PX } from '@/lib/wheelReplay';

const VIEW = { width: 800, height: 600 };
const LEFT = 100;
const TOP = 80;

function mount(enabled = true, viewSize = VIEW) {
    const el = document.createElement('div');
    document.body.appendChild(el);
    el.getBoundingClientRect = () => ({
        left: LEFT, top: TOP, right: LEFT + VIEW.width, bottom: TOP + VIEW.height,
        width: VIEW.width, height: VIEW.height, x: LEFT, y: TOP, toJSON: () => ({}),
    });
    const hook = renderHook(({ size }) => usePanZoomCamera({ rootDivRef: { current: el }, viewSize: size, enabled }), {
        initialProps: { size: viewSize },
    });
    return { el, ...hook };
}

const wheel = (el: HTMLElement, init: WheelEventInit) => {
    const e = new WheelEvent('wheel', { cancelable: true, ...init });
    act(() => { el.dispatchEvent(e); });
    return e;
};

/** The world point under a client point (StackScene's centre-origin transform). */
const worldAt = (cam: ReturnType<typeof usePanZoomCamera>, clientX: number, clientY: number) => ({
    x: (clientX - LEFT - VIEW.width / 2) / cam.zoomMV.get() + cam.cameraXMV.get(),
    y: (clientY - TOP - VIEW.height / 2) / cam.zoomMV.get() + VIEW.height / 2 + cam.cameraYMV.get(),
});

afterEach(() => { document.body.innerHTML = ''; });

describe('usePanZoomCamera: the overview wheel', () => {
    it('leaves native scrolling uncancelled outside the overview __F0612 (O01)', () => {
        const { el, result } = mount(false);
        const e = wheel(el, { deltaY: 100, ctrlKey: true });
        expect(e.defaultPrevented).toBe(false);
        expect(result.current.zoomMV.get()).toBe(1);
        expect(result.current.cameraYMV.get()).toBe(0);
    });

    it('listens on the overview\'s whole area: over the bar it pans, never zooms; over the board it zooms (O01, WZ-01)', () => {
        const area = document.createElement('div');
        const board = document.createElement('div');
        const bar = document.createElement('div');
        bar.setAttribute('data-rm-bar', '');
        const label = document.createElement('span');
        bar.append(label);
        area.append(board, bar);
        document.body.appendChild(area);
        const { result } = renderHook(() => usePanZoomCamera({
            rootDivRef: { current: board }, wheelRootRef: { current: area }, viewSize: VIEW, enabled: true,
            wheelPansSelector: '[data-rm-bar]',
        }));
        const e = wheel(label, { deltaY: 40, bubbles: true });
        expect(e.defaultPrevented).toBe(true);
        expect(result.current.cameraYMV.get()).toBe(40);
        expect(result.current.zoomMV.get()).toBe(1);
        // A pinch there is swallowed: neither the board nor the page zooms.
        const pinch = wheel(label, { deltaY: 40, ctrlKey: true, bubbles: true });
        expect(pinch.defaultPrevented).toBe(true);
        expect(result.current.zoomMV.get()).toBe(1);
        wheel(board, { deltaY: 100, bubbles: true });
        expect(result.current.zoomMV.get()).toBeCloseTo(1 / OVERVIEW_ZOOM_STEP);
    });

    it('a plain wheel zooms about the cursor, a 100px notch the zoom control\'s step (WZ-01)', () => {
        const { el, result } = mount();
        const clientX = 300, clientY = 250;
        const before = worldAt(result.current, clientX, clientY);
        const e = wheel(el, { deltaY: 100, clientX, clientY });
        expect(e.defaultPrevented).toBe(true);
        expect(result.current.zoomMV.get()).toBeCloseTo(0.8);
        const after = worldAt(result.current, clientX, clientY);
        expect(after.x).toBeCloseTo(before.x);
        expect(after.y).toBeCloseTo(before.y);
        wheel(el, { deltaY: -100, clientX, clientY });
        expect(result.current.zoomMV.get()).toBeCloseTo(1);
        // A line-mode notch (deltaMode 1) is a line's px.
        wheel(el, { deltaY: 1, deltaMode: 1, clientX, clientY });
        expect(result.current.zoomMV.get()).toBeCloseTo(Math.exp(-WHEEL_LINE_PX * WHEEL_ZOOM_PER_PX));
        // The same limits as the control and the pinch.
        for (let i = 0; i < 40; i++) wheel(el, { deltaY: 100, clientX, clientY });
        expect(result.current.zoomMV.get()).toBe(OVERVIEW_ZOOM.min);
        for (let i = 0; i < 40; i++) wheel(el, { deltaY: -100, clientX, clientY });
        expect(result.current.zoomMV.get()).toBe(OVERVIEW_ZOOM.max);
    });

    it('shift+wheel and a sideways wheel pan the board, both ways, at the current zoom (WZ-01)', () => {
        const { el, result } = mount();
        const e = wheel(el, { deltaX: 30, deltaY: 100, shiftKey: true });
        expect(e.defaultPrevented).toBe(true);
        expect(result.current.zoomMV.get()).toBe(1);
        expect(result.current.cameraXMV.get()).toBe(30);
        expect(result.current.cameraYMV.get()).toBe(100);
        act(() => { result.current.zoomMV.set(0.5); });
        wheel(el, { deltaY: 50, shiftKey: true });
        // 50 screen px at 50% is 100 world px.
        expect(result.current.cameraYMV.get()).toBe(200);
        wheel(el, { deltaY: 1, deltaMode: 1, shiftKey: true });
        expect(result.current.cameraYMV.get()).toBe(200 + WHEEL_LINE_PX / 0.5);
        // Chromium turns shift+wheel sideways; a tilt wheel or a trackpad's
        // horizontal swipe is sideways too: both pan.
        wheel(el, { deltaX: 40 });
        expect(result.current.cameraXMV.get()).toBe(30 + 40 / 0.5);
        expect(result.current.zoomMV.get()).toBe(0.5);
    });

    it('leaves the wheel to an inner element that can still scroll its way (WZ-01)', () => {
        const { el, result } = mount();
        const list = document.createElement('div');
        list.style.overflowY = 'auto';
        Object.defineProperties(list, {
            scrollHeight: { value: 400, configurable: true }, clientHeight: { value: 100, configurable: true },
        });
        const row = document.createElement('div');
        list.append(row);
        el.append(list);
        const down = wheel(row, { deltaY: 100, bubbles: true });
        expect(down.defaultPrevented).toBe(false);
        expect(result.current.zoomMV.get()).toBe(1);
        // At its top it cannot scroll up: the board zooms.
        wheel(row, { deltaY: -100, bubbles: true });
        expect(result.current.zoomMV.get()).toBeCloseTo(OVERVIEW_ZOOM_STEP);
        // Scrolled to its end it cannot scroll down: the board zooms.
        list.scrollTop = 300;
        wheel(row, { deltaY: 100, bubbles: true });
        expect(result.current.zoomMV.get()).toBeCloseTo(1);
    });

    it.each([['ctrl', { ctrlKey: true }], ['cmd', { metaKey: true }]] as const)(
        '%s+wheel zooms about the cursor, the world point under it staying put __F0612 (O01)', (_name, mods) => {
            const { el, result } = mount();
            const clientX = 300, clientY = 250;
            const before = worldAt(result.current, clientX, clientY);
            wheel(el, { deltaY: 100, clientX, clientY, ...mods });
            expect(result.current.zoomMV.get()).toBeCloseTo(Math.exp(-100 * PINCH_ZOOM_PER_PX));
            const after = worldAt(result.current, clientX, clientY);
            expect(after.x).toBeCloseTo(before.x);
            expect(after.y).toBeCloseTo(before.y);
            wheel(el, { deltaY: -50, clientX, clientY, ...mods });
            expect(result.current.zoomMV.get()).toBeCloseTo(Math.exp(-0.5));
        });

    it('a pinch (Chromium sends it as ctrl+wheel) zooms between 0.2 and 1.5 (O01)', () => {
        const { el, result } = mount();
        for (let i = 0; i < 20; i++) wheel(el, { deltaY: 200, ctrlKey: true, clientX: 500, clientY: 380 });
        expect(result.current.zoomMV.get()).toBe(OVERVIEW_ZOOM.min);
        expect(OVERVIEW_ZOOM.min).toBe(0.2);
        for (let i = 0; i < 20; i++) wheel(el, { deltaY: -200, ctrlKey: true, clientX: 500, clientY: 380 });
        expect(result.current.zoomMV.get()).toBe(OVERVIEW_ZOOM.max);
        expect(OVERVIEW_ZOOM.max).toBe(1.5);
    });

    it('zoomCameraAt (the zoom control) keeps the point it zooms about and clamps (O01)', () => {
        const { result } = mount();
        const cam = result.current;
        act(() => zoomCameraAt(cam, VIEW, 400, 300, 0.8));
        expect(cam.zoomMV.get()).toBeCloseTo(0.8);
        // The view's centre stays on the same world point.
        expect(cam.cameraXMV.get()).toBeCloseTo(0);
        expect(cam.cameraYMV.get()).toBeCloseTo(0);
        act(() => zoomCameraAt(cam, VIEW, 400, 300, 100));
        expect(cam.zoomMV.get()).toBe(1.5);
    });
});

describe('usePanZoomCamera: the drag pan', () => {
    const LEFT_BUTTON = 0, MIDDLE = 1, RIGHT = 2;
    const pointer = (el: HTMLElement, x: number, y: number, button = LEFT_BUTTON, target: Element = el) => {
        const e = {
            button, clientX: x, clientY: y, pointerId: 1, target, currentTarget: el,
            defaultPrevented: false, preventDefault: () => { e.defaultPrevented = true; },
        };
        return e as unknown as React.PointerEvent<HTMLDivElement> & { defaultPrevented: boolean };
    };
    /** A board with a card that drags on its own, the scene's panBlockSelector. */
    function mountWithCard(enabled = true) {
        const el = document.createElement('div');
        const card = document.createElement('article');
        card.setAttribute('data-stack-card', '');
        const body = document.createElement('div');
        card.append(body);
        el.append(card);
        document.body.appendChild(el);
        el.setPointerCapture = vi.fn();
        const { result } = renderHook(() => usePanZoomCamera({
            rootDivRef: { current: el }, viewSize: VIEW, enabled, panBlockSelector: '[data-stack-card]',
        }));
        return { el, body, result };
    }
    /** Everything a gesture did: the camera, the capture, the cursor while
     *  and after, the pan events, the click it reports. */
    function gesture(button: number, onCard: boolean, path: [number, number][], enabled = true) {
        const { el, body, result } = mountWithCard(enabled);
        el.style.cursor = 'default';
        act(() => { result.current.zoomMV.set(0.5); });
        const events: string[] = [];
        const log = (e: Event) => events.push(e.type);
        window.addEventListener('canvas-pan-start', log);
        window.addEventListener('canvas-pan-end', log);
        const target = onCard ? body : el;
        const [[x0, y0], ...rest] = path;
        const down = pointer(el, x0, y0, button, target);
        act(() => result.current.handlePanPointerDown(down));
        const cursors: string[] = [];
        for (const [x, y] of rest) {
            act(() => result.current.handlePanPointerMove(pointer(el, x, y, -1, target)));
            cursors.push(el.style.cursor);
        }
        const [xe, ye] = path.at(-1)!;
        let click: boolean | undefined;
        act(() => { click = result.current.handlePanPointerUp(pointer(el, xe, ye, button, target)); });
        window.removeEventListener('canvas-pan-start', log);
        window.removeEventListener('canvas-pan-end', log);
        return {
            cam: [result.current.cameraXMV.get(), result.current.cameraYMV.get()],
            captured: vi.mocked(el.setPointerCapture).mock.calls,
            cursors, cursorAfter: el.style.cursor, events, click, pressPrevented: down.defaultPrevented,
        };
    }
    const DRAG: [number, number][] = [[400, 300], [402, 301], [300, 240], [200, 180]];
    const CLICK: [number, number][] = [[400, 300], [401, 301]];

    it('the middle and right buttons pan anywhere, the left button not over a card that drags', () => {
        expect([...PAN_BUTTONS].sort()).toEqual([MIDDLE, RIGHT]);
    });

    it.each([['the background', false], ['a card that drags', true]] as const)(
        'over %s a middle drag is a right drag: 4px threshold, capture, grabbing cursor, 1:1 pan at the zoom, pan events, no click (O01)',
        (_where, onCard) => {
            const right = gesture(RIGHT, onCard, DRAG);
            // (-200, -120) screen px at 50% is (400, 240) world px against it.
            expect(right.cam).toEqual([400, 240]);
            expect(right.captured).toEqual([[1]]);
            // Under the threshold the cursor is the root's own; past it, grabbing.
            expect(right.cursors).toEqual(['default', 'grabbing', 'grabbing']);
            expect(right.cursorAfter).toBe('default');
            expect(right.events).toEqual(['canvas-pan-start', 'canvas-pan-end']);
            expect(right.click).toBe(false);
            const { pressPrevented, ...middle } = gesture(MIDDLE, onCard, DRAG);
            const { pressPrevented: _r, ...rightSame } = right;
            expect(middle).toEqual(rightSame);
            // The middle press is cancelled (no autoscroll, no paste); the
            // right press is left to the browser (its menu is the scene's).
            expect(pressPrevented).toBe(true);
            expect(right.pressPrevented).toBe(false);
        });

    it.each([['the background', false], ['a card that drags', true]] as const)(
        'over %s a plain middle click is a plain right click: no pan, no capture, a click (O01)', (_where, onCard) => {
            const right = gesture(RIGHT, onCard, CLICK);
            expect(right.cam).toEqual([0, 0]);
            expect(right.captured).toEqual([]);
            expect(right.events).toEqual([]);
            expect(right.click).toBe(true);
            const { pressPrevented: _m, ...middle } = gesture(MIDDLE, onCard, CLICK);
            const { pressPrevented: _r, ...rightSame } = right;
            expect(middle).toEqual(rightSame);
        });

    it('the left button pans the background but not from a card that drags (O01)', () => {
        expect(gesture(LEFT_BUTTON, false, DRAG).cam).toEqual([400, 240]);
        const onCard = gesture(LEFT_BUTTON, true, DRAG);
        expect(onCard).toMatchObject({ cam: [0, 0], captured: [], events: [], click: false, pressPrevented: false });
    });

    it.each([LEFT_BUTTON, MIDDLE, RIGHT])('button %i does nothing outside the overview, its press left alone (O01)', (button) => {
        expect(gesture(button, false, DRAG, false)).toMatchObject({ cam: [0, 0], captured: [], events: [], pressPrevented: false });
    });

    it('cancels the middle button\'s auxclick on the board in the overview only (O01)', () => {
        const aux = (el: HTMLElement, button: number) => {
            const e = new MouseEvent('auxclick', { button, bubbles: true, cancelable: true });
            el.dispatchEvent(e);
            return e.defaultPrevented;
        };
        const on = mountWithCard(true);
        expect(aux(on.body, MIDDLE)).toBe(true);
        expect(aux(on.el, MIDDLE)).toBe(true);
        expect(aux(on.el, RIGHT)).toBe(false);
        document.body.innerHTML = '';
        const off = mountWithCard(false);
        expect(aux(off.el, MIDDLE)).toBe(false);
    });

    it('a press that stays put is a click, not a pan (O01)', () => {
        const { el, result } = mount();
        act(() => result.current.handlePanPointerDown(pointer(el, 100, 100)));
        act(() => result.current.handlePanPointerMove(pointer(el, 101, 101)));
        let click = false;
        act(() => { click = result.current.handlePanPointerUp(pointer(el, 101, 101)); });
        expect(click).toBe(true);
        expect(result.current.cameraXMV.get()).toBe(0);
    });
});

describe('usePanZoomCamera: the board region resizing', () => {
    it('keeps the board top-left on screen when the shelf or the journal pane resizes the region (O01)', () => {
        const { result, rerender } = mount(true, { width: 800, height: 600 });
        act(() => { result.current.zoomMV.set(0.5); result.current.cameraXMV.set(100); result.current.cameraYMV.set(40); });
        // Board-local screen position of a world point, for a view size.
        const at = (view: { width: number; height: number }, x: number, y: number) => ({
            x: view.width / 2 + 0.5 * (x - result.current.cameraXMV.get()),
            y: view.height / 2 + 0.5 * (y - result.current.cameraYMV.get() - view.height / 2),
        });
        // The overview's world stays put through a resize (useStackLayout).
        const before = at({ width: 800, height: 600 }, 50, 60);
        rerender({ size: { width: 460, height: 284 } });
        const after = at({ width: 460, height: 284 }, 50, 60);
        expect(after.x).toBeCloseTo(before.x);
        expect(after.y).toBeCloseTo(before.y);
    });

    it('keeps the zoom and the board\'s content against the region\'s left edge every step of a Commands pane narrowing it, so a pane docked left slides it right by its width and one docked right leaves it (polish)', () => {
        const { result, rerender } = mount(true, { width: 1200, height: 600 });
        act(() => { result.current.zoomMV.set(0.4); result.current.cameraXMV.set(-120); result.current.cameraYMV.set(30); });
        const cam = () => ({ x: result.current.cameraXMV.get(), y: result.current.cameraYMV.get() });
        // Region-local screen x of a world x (the overview's world stays put
        // through a resize: useStackLayout).
        const localX = (width: number, x: number) => width / 2 + 0.4 * (x - result.current.cameraXMV.get());
        const before = [-600, 0, 800].map(x => localX(1200, x));
        const y0 = cam().y;
        // The pane's 160ms slide, a frame at a time, open and shut again.
        const widths = [1168, 1120, 1040, 960, 900, 880, 940, 1060, 1200];
        for (const width of widths) {
            rerender({ size: { width, height: 600 } });
            expect(result.current.zoomMV.get()).toBe(0.4);
            expect(cam().y).toBe(y0);
            // Region-local, nothing moves: docked right the region's left
            // edge stays, so the content does on screen; docked left that
            // edge carries it right by exactly what the pane covers.
            [-600, 0, 800].forEach((x, i) => expect(localX(width, x)).toBeCloseTo(before[i], 6));
        }
    });

    it('leaves the camera alone outside the overview (O01)', () => {
        const { result, rerender } = mount(false, { width: 800, height: 600 });
        rerender({ size: { width: 400, height: 300 } });
        expect(result.current.cameraXMV.get()).toBe(0);
        expect(result.current.cameraYMV.get()).toBe(0);
    });
});
