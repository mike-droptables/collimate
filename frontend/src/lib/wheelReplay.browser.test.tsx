import { expect, it } from 'vitest';
import { useCallback, useState } from 'react';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import { attachScrimWheel } from './wheelReplay';

// Real Chromium: a non-passive listener on the wheel's path (the canvas
// swipe's) makes the wheel blocking, and the browser hit-tests its native
// scroll only after the page handled it. By then the selection commit has
// removed the scrim and flipped the shadow host, so the native scroll lands
// in the shadow scroller; a replay on top of it scrolled the first notch
// twice. The scrim cancels the wheel it replays, so the travel lands once.
it('a blocking first wheel on a shadow host\'s scrim scrolls the flipped host by its travel, once', async () => {
  let shadow: ShadowRoot | null = null;
  const twoFrames = (run: () => void) => requestAnimationFrame(() => requestAnimationFrame(run));
  function Cell() {
    const [active, setActive] = useState(false);
    const hostRef = useCallback((el: HTMLDivElement | null) => {
      if (!el || el.shadowRoot) return;
      shadow = el.attachShadow({ mode: 'open' });
      const column = document.createElement('div');
      column.style.cssText = 'height:100%;overflow-y:auto';
      const tall = document.createElement('div');
      tall.style.height = '2000px';
      column.append(tall);
      shadow.append(column);
    }, []);
    const blockingRef = useCallback((el: HTMLDivElement | null) => {
      if (!el) return;
      const swipe = () => {};
      el.addEventListener('wheel', swipe, { passive: false });
      return () => el.removeEventListener('wheel', swipe);
    }, []);
    const scrimRef = useCallback((el: HTMLDivElement | null) => (
      el ? attachScrimWheel(el, () => shadow, twoFrames) : undefined
    ), []);
    // The card selects on the wheel; the host flips in that commit.
    return <div ref={blockingRef} onWheel={() => setActive(true)}
      style={{ width: 500, height: 240, position: 'relative', overflow: 'hidden' }}>
      <div ref={hostRef} style={{ height: '100%', pointerEvents: active ? 'auto' : 'none' }} />
      {!active && <div ref={scrimRef} title="scrim" style={{ position: 'absolute', inset: 0 }} />}
    </div>;
  }
  const view = await render(<Cell />);
  try {
    const scrim = page.getByTitle('scrim');
    await scrim.wheel({ delta: { y: 100 } });
    await expect.element(scrim).not.toBeInTheDocument();
    const column = shadow!.firstElementChild as HTMLElement;
    await expect.poll(() => column.scrollTop).toBe(100);
    // Settled: neither a late native scroll nor a second replay follows.
    await new Promise(resolve => setTimeout(resolve, 250));
    expect(column.scrollTop).toBe(100);
  } finally {
    await view.unmount();
  }
});

// Three notches in one burst: the first focuses and is replayed, the next
// two land after the focus commit removed the scrim. All three travel.
it('every notch of the burst that focuses the cell lands, none lost to the hand-off', async () => {
  let shadow: ShadowRoot | null = null;
  const twoFrames = (run: () => void) => requestAnimationFrame(() => requestAnimationFrame(run));
  function Cell() {
    const [active, setActive] = useState(false);
    const hostRef = useCallback((el: HTMLDivElement | null) => {
      if (!el || el.shadowRoot) return;
      shadow = el.attachShadow({ mode: 'open' });
      const column = document.createElement('div');
      column.style.cssText = 'height:100%;overflow-y:auto';
      const tall = document.createElement('div');
      tall.style.height = '2000px';
      column.append(tall);
      shadow.append(column);
    }, []);
    const scrimRef = useCallback((el: HTMLDivElement | null) => (
      el ? attachScrimWheel(el, () => shadow, twoFrames) : undefined
    ), []);
    return <div title="cell" onWheel={() => setActive(true)}
      style={{ width: 500, height: 240, position: 'relative', overflow: 'hidden' }}>
      <div ref={hostRef} style={{ height: '100%', pointerEvents: active ? 'auto' : 'none' }} />
      {!active && <div ref={scrimRef} title="scrim" style={{ position: 'absolute', inset: 0 }} />}
    </div>;
  }
  const view = await render(<Cell />);
  try {
    const cell = page.getByTitle('cell');
    await page.getByTitle('scrim').wheel({ delta: { y: 100 } });
    await cell.wheel({ delta: { y: 100 } });
    await cell.wheel({ delta: { y: 100 } });
    const column = shadow!.firstElementChild as HTMLElement;
    await expect.poll(() => column.scrollTop, { timeout: 2000 }).toBe(300);
    await new Promise(resolve => setTimeout(resolve, 250));
    expect(column.scrollTop).toBe(300);
  } finally {
    await view.unmount();
  }
});
