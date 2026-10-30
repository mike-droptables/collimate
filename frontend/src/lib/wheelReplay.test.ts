// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
    WHEEL_LINE_PX, attachScrimWheel, createWheelReplay, isFocusWheel, opaqueFrameAt, scrollTargetAt,
    scrollTargetInFrame,
} from './wheelReplay';

afterEach(() => { document.body.innerHTML = ''; });

/** A scroll container: `overflow` on the axis, content `extent` px longer
 *  than its 100px box. */
function scroller(axis: 'x' | 'y', extent = 300): HTMLDivElement {
    const el = document.createElement('div');
    if (axis === 'y') {
        el.style.overflowY = 'auto';
        Object.defineProperties(el, { scrollHeight: { value: 100 + extent }, clientHeight: { value: 100 } });
    } else {
        el.style.overflowX = 'auto';
        Object.defineProperties(el, { scrollWidth: { value: 100 + extent }, clientWidth: { value: 100 } });
    }
    return el;
}

/** A shadow root on a host in the document, hit-testing to `hit`. */
function shadowHitting(build: (root: ShadowRoot) => Element): { root: ShadowRoot; host: HTMLElement } {
    const host = document.createElement('div');
    document.body.append(host);
    const root = host.attachShadow({ mode: 'open' });
    const hit = build(root);
    Object.defineProperty(root, 'elementFromPoint', { value: vi.fn(() => hit) });
    return { root, host };
}

describe('isFocusWheel', () => {
    it('is the vertical, non-pinch wheel: the complement of the canvas swipe', () => {
        expect(isFocusWheel({ deltaX: 0, deltaY: 30 })).toBe(true);
        expect(isFocusWheel({ deltaX: 10, deltaY: -30 })).toBe(true);
        expect(isFocusWheel({ deltaX: 20, deltaY: 20 })).toBe(true);
        expect(isFocusWheel({ deltaX: 60, deltaY: 10 })).toBe(false);
        expect(isFocusWheel({ deltaX: 0, deltaY: 30, ctrlKey: true })).toBe(false);
    });
});

describe('scrollTargetAt', () => {
    it('walks out from the hit element to the nearest scroller that can still travel that way', () => {
        let inner!: HTMLDivElement;
        let outer!: HTMLDivElement;
        let leaf!: HTMLSpanElement;
        const { root } = shadowHitting(r => {
            outer = scroller('y');
            inner = scroller('y');
            leaf = document.createElement('span');
            inner.append(leaf);
            outer.append(inner);
            r.append(outer);
            return leaf;
        });
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBe(inner);
        // At its bottom edge the inner one chains to the outer, as natively.
        inner.scrollTop = 300;
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBe(outer);
        // ...and still takes travel back up.
        expect(scrollTargetAt(root, 5, 5, 0, -40)).toBe(inner);
        // A horizontal scroller does not take vertical travel.
        expect(scrollTargetAt(root, 5, 5, 40, 0)).toBeNull();
    });

    it('never reaches past the root, and finds nothing where nothing scrolls', () => {
        const page = scroller('y');
        document.body.append(page);
        const host = document.createElement('div');
        page.append(host);
        const root = host.attachShadow({ mode: 'open' });
        const plain = document.createElement('div');
        root.append(plain);
        const hitTest = vi.fn<() => Element | null>(() => plain);
        Object.defineProperty(root, 'elementFromPoint', { value: hitTest });
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBeNull();
        hitTest.mockReturnValue(null);
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBeNull();
        // A shadow root whose host is still pointer-events:none answers with
        // the light-DOM element on top: not the renderer's to scroll.
        hitTest.mockReturnValue(page);
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBeNull();
    });

    it('descends into a nested shadow root and walks back out through its host', () => {
        let outer!: HTMLDivElement;
        let inner!: HTMLDivElement;
        const { root } = shadowHitting(r => {
            outer = scroller('y');
            const widget = document.createElement('div');
            outer.append(widget);
            r.append(outer);
            const nested = widget.attachShadow({ mode: 'open' });
            inner = scroller('y');
            const text = document.createElement('p');
            inner.append(text);
            nested.append(inner);
            Object.defineProperty(nested, 'elementFromPoint', { value: () => text });
            return widget;
        });
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBe(inner);
        inner.scrollTop = 300;
        expect(scrollTargetAt(root, 5, 5, 0, 40)).toBe(outer);
    });
});

describe('scrollTargetInFrame', () => {
    function frameAt(left: number, top: number): HTMLIFrameElement {
        const frame = document.createElement('iframe');
        document.body.append(frame);
        frame.getBoundingClientRect = () => ({ left, top, width: 0, height: 0 } as DOMRect);
        return frame;
    }

    it('hit-tests the frame\'s own document at frame-local coordinates', () => {
        const frame = frameAt(100, 50);
        const doc = frame.contentDocument!;
        const target = scroller('y');
        doc.body.append(target);
        const hitTest = vi.fn(() => target);
        Object.defineProperty(doc, 'elementFromPoint', { value: hitTest });
        expect(scrollTargetInFrame(frame, 130, 90, 0, 40)).toBe(target);
        expect(hitTest).toHaveBeenCalledWith(30, 40);
    });

    it('is null for an opaque-origin frame, whose document the host cannot see', () => {
        const frame = frameAt(0, 0);
        Object.defineProperty(frame, 'contentDocument', { value: null });
        expect(scrollTargetInFrame(frame, 10, 10, 0, 40)).toBeNull();
    });

    it('a frame hit inside a root is followed into', () => {
        const host = document.createElement('div');
        document.body.append(host);
        const root = host.attachShadow({ mode: 'open' });
        // jsdom loads no frame inside a shadow root: borrow a loaded one's document.
        const doc = frameAt(0, 0).contentDocument!;
        const frame = document.createElement('iframe');
        root.append(frame);
        Object.defineProperty(frame, 'contentDocument', { value: doc });
        frame.getBoundingClientRect = () => ({ left: 10, top: 10, width: 0, height: 0 } as DOMRect);
        const target = scroller('y');
        doc.body.append(target);
        Object.defineProperty(doc, 'elementFromPoint', { value: () => target });
        Object.defineProperty(root, 'elementFromPoint', { value: () => frame });
        expect(scrollTargetAt(root, 20, 20, 0, 40)).toBe(target);
    });
});

describe('createWheelReplay', () => {
    const wheel = (over: Partial<WheelEvent> = {}) => ({
        deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, clientX: 0, clientY: 0, ...over,
    });
    /** An injectable frame source: queued callbacks, run with a clock. */
    const frames = () => {
        const queue: Array<(now: number) => void> = [];
        let now = 1000;
        const one = () => { const run = queue.shift(); if (run) run(now += 1000 / 60); };
        return {
            frame: (run: (now: number) => void) => { queue.push(run); },
            one,
            /** Run queued frames 16.7ms apart until the animation stops asking. */
            settle: () => { let guard = 0; while (queue.length && guard++ < 500) one(); },
            queued: () => queue.length,
        };
    };
    const wheelAt = (over: WheelEventInit = {}) =>
        new WheelEvent('wheel', { bubbles: true, cancelable: true, composed: true, clientX: 10, clientY: 10, ...over });

    it('folds the wheels before a deferred replay into one animation from where the content stood', () => {
        const target = scroller('y');
        const resolve = vi.fn(() => target);
        const runs: Array<() => void> = [];
        const f = frames();
        const replay = createWheelReplay(resolve, run => runs.push(run), { frame: f.frame });

        replay(wheel({ deltaY: 30, clientX: 10, clientY: 20 }));
        replay(wheel({ deltaY: 2, deltaMode: 1, clientX: 11, clientY: 21 }));
        replay(wheel({ deltaY: 5, deltaX: 1, clientX: 12, clientY: 22 }));
        expect(runs).toHaveLength(1);
        expect(replay.active()).toBe(true);
        expect(target.scrollTop).toBe(0);
        runs[0]();
        expect(resolve).toHaveBeenCalledExactlyOnceWith(12, 22, 1, 30 + 2 * WHEEL_LINE_PX + 5);
        f.settle();
        expect(target.scrollTop).toBe(30 + 2 * WHEEL_LINE_PX + 5);
        expect(replay.active()).toBe(false);

        replay(wheel({ deltaY: -10 }));
        expect(runs).toHaveLength(2);
        runs[1]();
        f.settle();
        expect(target.scrollTop).toBe(30 + 2 * WHEEL_LINE_PX + 5 - 10);
    });

    it('animates rather than jumps: the first frame covers only part of the travel', () => {
        const target = scroller('y');
        const f = frames();
        const replay = createWheelReplay(() => target, run => run(), { frame: f.frame });
        replay(wheel({ deltaY: 100 }));
        expect(target.scrollTop).toBe(0);
        f.one();
        expect(target.scrollTop).toBeGreaterThan(0);
        expect(target.scrollTop).toBeLessThan(50);
        f.settle();
        expect(target.scrollTop).toBe(100);
    });

    it('a notch after the replay, over the cell, folds into the running animation; once settled, wheels pass', () => {
        const wrapper = document.createElement('div');
        document.body.append(wrapper);
        const target = scroller('y');
        wrapper.append(target);
        const f = frames();
        const runs: Array<() => void> = [];
        const replay = createWheelReplay(() => target, run => runs.push(run), { frame: f.frame, within: () => wrapper });
        try {
            replay(wheel({ deltaY: 40 }));
            // Between the focus commit and the replay: a notch lands on the
            // wrapper, not the scrim. Folded from the document, cancelled.
            const second = wheelAt({ deltaY: 30 });
            target.dispatchEvent(second);
            expect(second.defaultPrevented).toBe(true);
            runs[0]();
            // During the animation: a third notch extends the goal.
            const third = wheelAt({ deltaY: 20 });
            target.dispatchEvent(third);
            expect(third.defaultPrevented).toBe(true);
            // A sideways wheel (the swipe's) and one elsewhere pass.
            const sideways = wheelAt({ deltaX: 60, deltaY: 5 });
            target.dispatchEvent(sideways);
            expect(sideways.defaultPrevented).toBe(false);
            const elsewhere = wheelAt({ deltaY: 20 });
            document.body.dispatchEvent(elsewhere);
            expect(elsewhere.defaultPrevented).toBe(false);
            f.settle();
            expect(target.scrollTop).toBe(90);
            expect(replay.active()).toBe(false);
            const after = wheelAt({ deltaY: 20 });
            target.dispatchEvent(after);
            expect(after.defaultPrevented).toBe(false);
            expect(target.scrollTop).toBe(90);
        } finally { replay.cancel(); }
    });

    it('a burst never aims past the scroller\'s end', () => {
        const target = scroller('y', 50);
        const f = frames();
        const replay = createWheelReplay(() => target, run => run(), { frame: f.frame });
        replay(wheel({ deltaY: 400 }));
        f.settle();
        expect(target.scrollTop).toBe(50);
    });

    it('replays at once with an immediate defer, and sizes page wheels by the target', () => {
        const target = scroller('y');
        const f = frames();
        const replay = createWheelReplay(() => target, run => run(), { frame: f.frame });
        replay(wheel({ deltaY: 40 }));
        f.settle();
        expect(target.scrollTop).toBe(40);
        replay(wheel({ deltaY: 1, deltaMode: 2 }));
        f.settle();
        expect(target.scrollTop).toBe(140);
    });

    it('replays only a focusing wheel: the swipe and the pinch are not scrolled into the cell', () => {
        const resolve = vi.fn(() => null);
        const defer = vi.fn((run: () => void) => run());
        const replay = createWheelReplay(resolve, defer);
        replay(wheel({ deltaX: 60, deltaY: 10 }));
        replay(wheel({ deltaY: 30, ctrlKey: true }));
        expect(defer).not.toHaveBeenCalled();
        replay(wheel({ deltaY: 30 }));
        expect(resolve).toHaveBeenCalledTimes(1);
    });
});

describe('opaqueFrameAt', () => {
    it('is true only over a frame whose document the host cannot see', () => {
        const host = document.createElement('div');
        document.body.append(host);
        const root = host.attachShadow({ mode: 'open' });
        const at = (frame: HTMLIFrameElement, left: number, top: number) => {
            frame.getBoundingClientRect = () => ({ left, top, right: left + 100, bottom: top + 100 } as DOMRect);
            root.append(frame);
            return frame;
        };
        const opaque = at(document.createElement('iframe'), 0, 0);
        Object.defineProperty(opaque, 'contentDocument', { value: null });
        const visible = at(document.createElement('iframe'), 200, 0);
        Object.defineProperty(visible, 'contentDocument', { value: document.implementation.createHTMLDocument() });
        expect(opaqueFrameAt(root, 50, 50)).toBe(true);
        expect(opaqueFrameAt(root, 250, 50)).toBe(false);
        expect(opaqueFrameAt(root, 150, 50)).toBe(false);
        expect(opaqueFrameAt(root, 50, 100)).toBe(false);
    });
});

describe('attachScrimWheel', () => {
    const wheelAt = (over: WheelEventInit = {}) =>
        new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: 10, clientY: 10, ...over });

    it('cancels the native scroll of each focus wheel it replays, and nothing else', () => {
        const target = scroller('y');
        const { root } = shadowHitting(r => { r.append(target); return target; });
        const scrim = document.createElement('div');
        document.body.append(scrim);
        const runs: Array<() => void> = [];
        const queue: Array<(now: number) => void> = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(run => { queue.push(run); return queue.length; });
        const detach = attachScrimWheel(scrim, () => root, run => runs.push(run));

        const vertical = wheelAt({ deltaY: 40 });
        const sideways = wheelAt({ deltaX: 60, deltaY: 5 });
        const pinch = wheelAt({ deltaY: 40, ctrlKey: true });
        for (const w of [vertical, sideways, pinch]) scrim.dispatchEvent(w);
        expect([vertical, sideways, pinch].map(w => w.defaultPrevented)).toEqual([true, false, false]);
        expect(runs).toHaveLength(1);
        runs[0]();
        let now = 0;
        while (queue.length) queue.shift()!(now += 1000 / 60);
        expect(target.scrollTop).toBe(40);

        detach();
        const after = wheelAt({ deltaY: 40 });
        scrim.dispatchEvent(after);
        expect(after.defaultPrevented).toBe(false);
        expect(runs).toHaveLength(1);
    });

    it('leaves a wheel over an opaque frame to the native scroll: no replay could reach it', () => {
        const host = document.createElement('div');
        document.body.append(host);
        const root = host.attachShadow({ mode: 'open' });
        const frame = document.createElement('iframe');
        Object.defineProperty(frame, 'contentDocument', { value: null });
        frame.getBoundingClientRect = () => ({ left: 0, top: 0, right: 100, bottom: 100 } as DOMRect);
        root.append(frame);
        const scrim = document.createElement('div');
        document.body.append(scrim);
        const defer = vi.fn();
        attachScrimWheel(scrim, () => root, defer);
        const over = wheelAt({ deltaY: 40 });
        scrim.dispatchEvent(over);
        expect(over.defaultPrevented).toBe(false);
        expect(defer).not.toHaveBeenCalled();
        const beside = wheelAt({ deltaY: 40, clientX: 150 });
        scrim.dispatchEvent(beside);
        expect(beside.defaultPrevented).toBe(true);
        expect(defer).toHaveBeenCalledTimes(1);
    });
});
