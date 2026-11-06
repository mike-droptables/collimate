import { afterEach, expect, it } from 'vitest';
import { adoptScrollbars, installScrollActivity, installScrollbars, SCROLLING_ATTR } from './scrollbars';

const stops: Array<() => void> = [];
afterEach(() => {
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

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

it('fades through intermediate thumb opacity, clears while idle, and reappears on pointer movement', async () => {
    adoptScrollbars(document);
    const box = scrollBox(document.body);
    // A short interval keeps the browser test quick; production uses the
    // same listener with a longer idle interval.
    stops.push(installScrollActivity(document, 600));
    expect(opacity(box)).toBe(0);
    box.dispatchEvent(new Event('scroll'));
    await wait(90);
    expect(opacity(box)).toBeGreaterThan(0);
    expect(opacity(box)).toBeLessThan(100);
    expect(getComputedStyle(box, '::-webkit-scrollbar-thumb').backgroundColor).toContain(' / ');
    await wait(600);
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(false);
    expect(opacity(box)).toBeGreaterThan(0);
    expect(opacity(box)).toBeLessThan(100);
    await wait(450);
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
    await wait(220);
    expect(opacity(box)).toBe(100);
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
