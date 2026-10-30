// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
    DRAGGING_ATTR, MIN_THUMB_PX, PAGE_FRACTION, SCROLLBAR_CSS, SCROLLBAR_SIZE_PX, SCROLLING_ATTR, SCROLL_IDLE_MS, type ScrollMetrics,
    adoptScrollbars, dragScroll, gutterAt, guttersOf, installScrollActivity, installScrollbars, installThumbDrag, scrollTargetOf,
} from './scrollbars';

describe('the workbench scrollbar sheet', () => {
    it('uses a 16px gutter and a skin-coloured thumb driven by activity opacity', () => {
        expect(SCROLLBAR_SIZE_PX).toBe(16);
        expect(SCROLLBAR_CSS).toMatch(/::-webkit-scrollbar\s*\{[^}]*width:\s*16px/);
        expect(SCROLLBAR_CSS).toMatch(/::-webkit-scrollbar-thumb\s*\{[^}]*border:\s*4px solid transparent/);
        expect(SCROLLBAR_CSS).toMatch(/@property --collimate-scrollbar-opacity/);
        expect(SCROLLBAR_CSS).toContain(`color-mix(in srgb, var(--line-2) var(--collimate-scrollbar-opacity), transparent)`);
        expect(SCROLLBAR_CSS).toMatch(new RegExp(`::-webkit-scrollbar-thumb\\s*\\{[^}]*min-height:\\s*${MIN_THUMB_PX}px`));
        expect(SCROLLBAR_CSS).toMatch(/::-webkit-scrollbar-thumb:hover\s*\{[^}]*var\(--ink-3\)/);
        expect(SCROLLBAR_CSS).toMatch(/\[data-scrollbar-dragging\]::-webkit-scrollbar-thumb\s*\{[^}]*var\(--ink-2\)/);
        expect(SCROLLBAR_CSS).toMatch(/scrollbar-width:\s*auto/);
        expect(SCROLLBAR_CSS).not.toMatch(/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/);
        expect(SCROLLBAR_CSS).toContain(`[${SCROLLING_ATTR}]`);
        expect(SCROLLBAR_CSS).toContain(`[${DRAGGING_ATTR}]`);
    });
});

// A 300×200 box with 1px borders, an 8px gutter on each side, content twice its
// size on both axes, scrolled a little.
const BOX: ScrollMetrics = {
    left: 100, top: 50, offsetWidth: 310, offsetHeight: 210, clientLeft: 1, clientTop: 1, borderRight: 1, borderBottom: 1,
    clientWidth: 300, clientHeight: 200, scrollWidth: 600, scrollHeight: 400, scrollLeft: 60, scrollTop: 100,
    overflowX: 'auto', overflowY: 'auto', barWidth: 8, barHeight: 8,
};

describe('the gutter geometry', () => {
    it('places each gutter along its edge, the thumb a proportion of the track at the scroll\'s place', () => {
        const { x, y } = guttersOf(BOX);
        expect(y).toMatchObject({ axis: 'y', trackStart: 51, trackLength: 200, thumbLength: 100, maxScroll: 200, scroll: 100, band: [401, 409] });
        expect(y!.thumbStart).toBe(51 + 100 * 100 / 200);     // half the room, for half the scroll
        expect(x).toMatchObject({ axis: 'x', trackStart: 101, trackLength: 300, thumbLength: 150, maxScroll: 300, scroll: 60, band: [251, 259] });
        expect(x!.thumbStart).toBe(101 + 150 * 60 / 300);
    });

    it('a thumb is never shorter than the sheet\'s minimum, and no gutter is reported where nothing overflows or no bar is drawn', () => {
        const tall = guttersOf({ ...BOX, scrollHeight: 100000 }).y!;
        expect(tall.thumbLength).toBe(MIN_THUMB_PX);
        expect(guttersOf({ ...BOX, scrollHeight: 200, scrollWidth: 300 })).toEqual({});
        expect(guttersOf({ ...BOX, overflowX: 'hidden', overflowY: 'hidden', offsetWidth: 302, offsetHeight: 202 })).toEqual({});
        expect(guttersOf({ ...BOX, overflowY: 'hidden', offsetWidth: 302 }).y).toBeUndefined();        // clipped: no bar, whatever the sheet says
    });

    it('a bar that takes no room (overlay scrollbars) lies over the content: the gutter is the bar\'s thickness along the edge', () => {
        const area: ScrollMetrics = { ...BOX, offsetWidth: 302, offsetHeight: 202, overflowX: 'hidden', scrollWidth: 300 };
        const { x, y } = guttersOf(area);
        expect(x).toBeUndefined();
        expect(y).toMatchObject({ axis: 'y', band: [100 + 302 - 1 - 8, 100 + 302 - 1], trackStart: 51, trackLength: 200 });
        expect(gutterAt(area, 398, 130)).toMatchObject({ onThumb: true });
    });

    it('a point in the vertical gutter is on the thumb or beside it; the corner belongs to neither gutter', () => {
        expect(gutterAt(BOX, 405, 130)).toMatchObject({ onThumb: true, along: 130, gutter: { axis: 'y' } });
        expect(gutterAt(BOX, 405, 60)).toMatchObject({ onThumb: false });
        expect(gutterAt(BOX, 405, 255)).toBeNull();                    // the corner, below the vertical track
        expect(gutterAt(BOX, 200, 255)).toMatchObject({ onThumb: true, gutter: { axis: 'x' } });
        expect(gutterAt(BOX, 200, 130)).toBeNull();                    // the content
        expect(gutterAt(BOX, 395, 130)).toBeNull();                    // just inside the gutter's edge
    });

    it('a dragged thumb follows the pointer across the track\'s room, clamped at both ends', () => {
        const y = guttersOf(BOX).y!;
        expect(dragScroll(y, 130, 130 - y.thumbStart)).toBe(100);        // held where it was grabbed: no move
        expect(dragScroll(y, 180, 130 - y.thumbStart)).toBe(200);        // 50px down over a 100px room: the whole 200px of scroll
        expect(dragScroll(y, 1000, 0)).toBe(200);
        expect(dragScroll(y, -1000, 0)).toBe(0);
        expect(dragScroll({ ...y, maxScroll: 0 }, 180, 0)).toBe(y.scroll);
    });
});

function box(size: { offsetWidth: number; offsetHeight: number } = BOX): HTMLElement {
    const el = document.createElement('div');
    document.body.appendChild(el);
    Object.defineProperties(el, {
        getBoundingClientRect: { value: () => ({ left: BOX.left, top: BOX.top, right: BOX.left + size.offsetWidth, bottom: BOX.top + size.offsetHeight, width: size.offsetWidth, height: size.offsetHeight }) },
        offsetWidth: { value: size.offsetWidth }, offsetHeight: { value: size.offsetHeight },
        clientLeft: { value: BOX.clientLeft }, clientTop: { value: BOX.clientTop },
        clientWidth: { value: BOX.clientWidth }, clientHeight: { value: BOX.clientHeight },
        scrollWidth: { value: BOX.scrollWidth }, scrollHeight: { value: BOX.scrollHeight },
    });
    // A border width computes to 0 without a style.
    el.style.borderRight = '1px solid'; el.style.borderBottom = '1px solid'; el.style.overflowX = 'auto'; el.style.overflowY = 'auto';
    el.scrollTop = BOX.scrollTop; el.scrollLeft = BOX.scrollLeft;
    el.scrollTo = ((opts: ScrollToOptions) => { if (opts.top !== undefined) el.scrollTop = opts.top; if (opts.left !== undefined) el.scrollLeft = opts.left; }) as typeof el.scrollTo;
    return el;
}

function pointer(type: string, x: number, y: number, extra: PointerEventInit = {}): PointerEvent {
    return new PointerEvent(type, { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, button: 0, pointerId: 1, pointerType: 'mouse', ...extra });
}

describe('installThumbDrag', () => {
    afterEach(() => { document.body.innerHTML = ''; });

    it('a press on the thumb captures the pointer and moves the scroll with it, anywhere, until the release, where it stays', () => {
        const el = box();
        const captured: number[] = [];
        el.setPointerCapture = (id: number) => { captured.push(id); };
        el.releasePointerCapture = () => {};
        const stop = installThumbDrag(document);
        const down = pointer('pointerdown', 405, 130);
        el.dispatchEvent(down);
        expect(down.defaultPrevented).toBe(true);
        expect(captured).toEqual([1]);
        expect(el.hasAttribute(DRAGGING_ATTR)).toBe(true);
        el.dispatchEvent(pointer('pointermove', 20, 155));             // far to the left of the cell: still the thumb's
        expect(el.scrollTop).toBe(150);
        el.dispatchEvent(pointer('pointermove', -300, 180));
        expect(el.scrollTop).toBe(200);
        el.dispatchEvent(pointer('pointermove', 900, 165));
        expect(el.scrollTop).toBe(170);
        el.dispatchEvent(pointer('pointerup', 900, 165));
        expect(el.hasAttribute(DRAGGING_ATTR)).toBe(false);
        expect(el.scrollTop).toBe(170);
        el.dispatchEvent(pointer('pointermove', 405, 60));
        expect(el.scrollTop).toBe(170);                               // released: no longer follows
        stop();
    });

    it('a press on the track pages toward the pointer, and a press on the content is left alone', () => {
        const el = box();
        const stop = installThumbDrag(document);
        const above = pointer('pointerdown', 405, 60);
        el.dispatchEvent(above);
        expect(above.defaultPrevented).toBe(true);
        expect(el.scrollTop).toBe(100 - 200 * PAGE_FRACTION < 0 ? 0 : 100 - 200 * PAGE_FRACTION);
        el.scrollTop = 0;
        el.dispatchEvent(pointer('pointerdown', 405, 240));
        expect(el.scrollTop).toBe(200 * PAGE_FRACTION);
        const content = pointer('pointerdown', 200, 130);
        el.dispatchEvent(content);
        expect(content.defaultPrevented).toBe(false);
        expect(el.hasAttribute(DRAGGING_ATTR)).toBe(false);
        stop();
    });

    it('the horizontal thumb drags along x; a touch or a secondary button is not a thumb drag', () => {
        const el = box();
        el.setPointerCapture = () => {}; el.releasePointerCapture = () => {};
        const stop = installThumbDrag(document);
        el.dispatchEvent(pointer('pointerdown', 200, 255));
        el.dispatchEvent(pointer('pointermove', 260, 600));           // 60px right over a 150px room: 120 of 300
        expect(el.scrollLeft).toBe(180);
        el.dispatchEvent(pointer('pointerup', 260, 600));
        const touch = pointer('pointerdown', 405, 130, { pointerType: 'touch', pointerId: 2 });
        el.dispatchEvent(touch);
        expect(touch.defaultPrevented).toBe(false);
        const right = pointer('pointerdown', 405, 130, { button: 2 });
        el.dispatchEvent(right);
        expect(right.defaultPrevented).toBe(false);
        stop();
    });

    it('a bar that takes no room lies over the content: a press on the child under it grabs the parent\'s thumb', () => {
        const el = box({ offsetWidth: BOX.clientWidth + 2, offsetHeight: BOX.clientHeight + 2 });   // borders only: the bar is over the content
        const child = document.createElement('p');
        el.appendChild(child);
        const real = window.getComputedStyle.bind(window);
        const spy = vi.spyOn(window, 'getComputedStyle').mockImplementation(((target: Element, pseudo?: string | null) =>
            pseudo === '::-webkit-scrollbar' ? ({ width: '8px', height: '8px' } as CSSStyleDeclaration) : real(target, pseudo)) as typeof window.getComputedStyle);
        el.setPointerCapture = () => {}; el.releasePointerCapture = () => {};
        const stop = installThumbDrag(document);
        try {
            const down = pointer('pointerdown', BOX.left + BOX.clientWidth + 2 - 4, 130);   // 4px in from the right border, where the bar lies
            child.dispatchEvent(down);
            expect(down.defaultPrevented).toBe(true);
            expect(el.hasAttribute(DRAGGING_ATTR)).toBe(true);
            expect(child.hasAttribute(DRAGGING_ATTR)).toBe(false);
            child.dispatchEvent(pointer('pointermove', 50, 180));
            expect(el.scrollTop).toBe(200);
            child.dispatchEvent(pointer('pointerup', 50, 180));
        } finally {
            stop();
            spy.mockRestore();
        }
    });

    it('the browser\'s own press on a gutter is cancelled too', () => {
        const el = box();
        const stop = installThumbDrag(document);
        const press = new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 405, clientY: 130, button: 0 });
        el.dispatchEvent(press);
        expect(press.defaultPrevented).toBe(true);
        const inside = new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 200, clientY: 130, button: 0 });
        el.dispatchEvent(inside);
        expect(inside.defaultPrevented).toBe(false);
        stop();
    });

    it('does not steal a press at the edge of a strip with its scrollbar hidden', () => {
        const el = box();
        el.style.scrollbarWidth = 'none';
        const stop = installThumbDrag(document);
        const press = pointer('pointerdown', 200, 255);
        el.dispatchEvent(press);
        expect(press.defaultPrevented).toBe(false);
        expect(el.hasAttribute(DRAGGING_ATTR)).toBe(false);
        expect(el.scrollLeft).toBe(BOX.scrollLeft);
        const mouse = new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 200, clientY: 255 });
        el.dispatchEvent(mouse);
        expect(mouse.defaultPrevented).toBe(false);
        stop();
    });
});

describe('installScrollActivity', () => {
    beforeEach(() => { vi.useFakeTimers(); });
    afterEach(() => { vi.useRealTimers(); document.body.innerHTML = ''; });

    it('reveals the element that scrolls and fades it after its last activity', () => {
        const el = document.createElement('div');
        document.body.appendChild(el);
        const stop = installScrollActivity(document);
        el.dispatchEvent(new Event('scroll'));
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        vi.advanceTimersByTime(SCROLL_IDLE_MS - 1);
        el.dispatchEvent(new Event('scroll'));          // still scrolling: the rest starts over
        vi.advanceTimersByTime(SCROLL_IDLE_MS - 1);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        vi.advanceTimersByTime(1);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(false);
        stop();
    });

    it('reveals on pointer entry and movement, then becomes idle even while hovered', () => {
        const el = box();
        const stop = installScrollActivity(document);
        el.dispatchEvent(pointer('pointerover', 200, 130));
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        vi.advanceTimersByTime(SCROLL_IDLE_MS);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(false);
        el.dispatchEvent(pointer('pointermove', 200, 130));
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        vi.advanceTimersByTime(SCROLL_IDLE_MS - 1);
        el.dispatchEvent(pointer('pointermove', 200, 130)); // Chromium can send a motionless move
        vi.advanceTimersByTime(1);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(false);
        el.dispatchEvent(pointer('pointermove', 201, 130));
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        stop();
    });

    it('keeps a held thumb visible and begins the idle interval after release', () => {
        const el = box();
        el.setPointerCapture = () => {};
        el.releasePointerCapture = () => {};
        const stop = installScrollbars(document);
        el.dispatchEvent(pointer('pointerover', 405, 130));
        el.dispatchEvent(pointer('pointerdown', 405, 130));
        expect(el.hasAttribute(DRAGGING_ATTR)).toBe(true);
        vi.advanceTimersByTime(SCROLL_IDLE_MS);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        el.dispatchEvent(pointer('pointerup', 405, 130));
        expect(el.hasAttribute(DRAGGING_ATTR)).toBe(false);
        vi.advanceTimersByTime(SCROLL_IDLE_MS - 1);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(true);
        vi.advanceTimersByTime(1);
        expect(el.hasAttribute(SCROLLING_ATTR)).toBe(false);
        stop();
    });

    it('a shadow root gets its own listener: a scroll inside it never reaches the document, and marks the scroller, not the host', () => {
        const host = document.createElement('div');
        document.body.appendChild(host);
        const shadow = host.attachShadow({ mode: 'open' });
        const inner = document.createElement('div');
        shadow.appendChild(inner);
        const stopDocument = installScrollActivity(document);
        inner.dispatchEvent(new Event('scroll'));
        expect(inner.hasAttribute(SCROLLING_ATTR)).toBe(false);
        const stopShadow = installScrollbars(shadow);
        inner.dispatchEvent(new Event('scroll'));
        expect(inner.hasAttribute(SCROLLING_ATTR)).toBe(true);
        expect(host.hasAttribute(SCROLLING_ATTR)).toBe(false);
        stopShadow();
        stopDocument();
        expect(inner.hasAttribute(SCROLLING_ATTR)).toBe(false);
    });

    it('a document scroll marks the root element', () => {
        const stop = installScrollActivity(document);
        document.dispatchEvent(new Event('scroll'));
        expect(document.documentElement.hasAttribute(SCROLLING_ATTR)).toBe(true);
        stop();
        expect(document.documentElement.hasAttribute(SCROLLING_ATTR)).toBe(false);
    });

    it('scrollTargetOf names the scroller', () => {
        const el = document.createElement('div');
        document.body.appendChild(el);
        let seen: Element | null = null;
        el.addEventListener('scroll', e => { seen = scrollTargetOf(e, document); });
        el.dispatchEvent(new Event('scroll'));
        expect(seen).toBe(el);
    });
});

describe('adoptScrollbars', () => {
    it('leaves a tree alone where no sheet can be constructed, and never throws', () => {
        const host = document.createElement('div');
        const shadow = host.attachShadow({ mode: 'open' });
        expect(() => adoptScrollbars(shadow)).not.toThrow();
        expect(() => adoptScrollbars(document)).not.toThrow();
    });
});
