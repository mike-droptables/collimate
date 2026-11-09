import { afterEach, expect, it, vi } from 'vitest';
import { adoptScrollbars, installScrollActivity, installScrollbars, SCROLLING_ATTR } from './scrollbars';

const stops: Array<() => void> = [];
afterEach(() => {
    vi.useRealTimers();
    for (const stop of stops.splice(0)) stop();
    document.body.innerHTML = '';
});

// Off the page's corner: a fresh page's pointer rests at (0, 0), and Chromium
// sends pointerover to a box that appears under it, which reveals the bar.
function scrollBox(parent: Node): HTMLElement {
    const box = document.createElement('div');
    box.style.cssText = 'width:100px;height:100px;margin:24px;overflow:auto;--line-2:#f00';
    const content = document.createElement('div');
    content.style.height = '500px';
    box.appendChild(content);
    parent.appendChild(box);
    return box;
}

function opacity(el: Element): number {
    return parseFloat(getComputedStyle(el).getPropertyValue('--collimate-scrollbar-opacity'));
}

// One synchronous reading of the activity mark, the animated opacity and the
// thumb colour, so a condition below holds of a single moment.
function sample(el: Element): { scrolling: boolean; opacity: number; thumb: string } {
    return {
        scrolling: el.hasAttribute(SCROLLING_ATTR),
        opacity: opacity(el),
        thumb: getComputedStyle(el, '::-webkit-scrollbar-thumb').backgroundColor,
    };
}

// Waits on the browser's animation clock, which a loaded machine delays,
// for a state rather than a fixed time.
const UNTIL = { timeout: 3000, interval: 4 };

// The element's running fade, paused half way: a reading taken now is of
// that one moment, however late the test gets to it on a loaded machine.
function pauseHalfway(el: Element): Animation {
    const [fade] = el.getAnimations();
    expect(fade).toBeDefined();
    fade.pause();
    fade.currentTime = Number(fade.effect!.getComputedTiming().duration) / 2;
    return fade;
}

// The idle interval runs on faked timers and each fade is paused half way, so
// no step races the fade-in's 180ms or the fade-out's 420ms.
it('fades through intermediate thumb opacity, clears while idle, and reappears on pointer movement', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    adoptScrollbars(document);
    const box = scrollBox(document.body);
    // A short interval keeps the browser test quick; production uses the
    // same listener with a longer idle interval.
    stops.push(installScrollActivity(document, 600));
    expect(opacity(box)).toBe(0);
    box.dispatchEvent(new Event('scroll'));
    // Fading in: marked, opacity part way, the thumb painted with alpha.
    const fadeIn = pauseHalfway(box);
    const rising = sample(box);
    expect(rising.scrolling).toBe(true);
    expect(rising.opacity).toBeGreaterThan(0);
    expect(rising.opacity).toBeLessThan(100);
    expect(rising.thumb).toContain(' / ');
    fadeIn.finish();
    expect(opacity(box)).toBe(100);
    // The idle interval clears the mark, and the fade out passes through
    // part opacity with the mark gone.
    vi.advanceTimersByTime(600);
    const fadeOut = pauseHalfway(box);
    const falling = sample(box);
    expect(falling.scrolling).toBe(false);
    expect(falling.opacity).toBeGreaterThan(0);
    expect(falling.opacity).toBeLessThan(100);
    fadeOut.finish();
    expect(opacity(box)).toBe(0);
    box.dispatchEvent(new PointerEvent('pointerover', { bubbles: true, pointerType: 'mouse' }));
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true);
});

it('installs the same activity behavior inside a native renderer shadow root', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = host.attachShadow({ mode: 'open' });
    adoptScrollbars(root);
    const box = scrollBox(root);
    const nested = scrollBox(box.firstElementChild!);
    stops.push(installScrollbars(root));
    box.dispatchEvent(new Event('scroll'));
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true);
    await vi.waitFor(() => expect(opacity(box)).toBe(100), UNTIL);
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true);
    expect(opacity(nested)).toBe(0);
    expect(getComputedStyle(box, '::-webkit-scrollbar-thumb').backgroundColor).not.toContain(' / 0)');
});

it('loads the committed scrollbar bundle in an iframe document', async () => {
    const frame = document.createElement('iframe');
    frame.srcdoc = '<style>:root{--line-2:#f00}</style><div id="box" style="height:100px;overflow:auto"><div style="height:500px"></div></div><script type="module" src="/scrollbars.js"></script>';
    document.body.appendChild(frame);
    await new Promise<void>(resolve => frame.addEventListener('load', () => resolve(), { once: true }));
    const inner = frame.contentDocument!;
    const box = inner.getElementById('box')!;
    expect(inner.adoptedStyleSheets.some(sheet => Array.from(sheet.cssRules).some(rule => rule.cssText.includes('--collimate-scrollbar-opacity')))).toBe(true);
    box.dispatchEvent(new Event('scroll'));
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true);
});
