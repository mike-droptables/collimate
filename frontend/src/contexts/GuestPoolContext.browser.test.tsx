/**
 * The guest pool's sync loop, in a real browser.
 *
 * jsdom can prove the wiring — that a release invalidates, that the pass
 * returns dirty — but it cannot prove the thing that actually matters:
 * that a pooled element tracks a container that is genuinely moving, and
 * that the loop genuinely stops when it isn't. jsdom has no layout, so
 * every rect is zero and every pass is clean forever.
 *
 * So: a real overlay, a real container, a real WAAPI animation (chosen
 * over a Motion animation on purpose — WAAPI fires no React commit and
 * no motion-value subscription, so the element can only be tracking
 * because the loop is running), and then the two halves of the contract:
 * the loop parks once the animation stops, and the verify tick still
 * catches a move that had no wake source at all.
 *
 * Run with VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { describe, it, expect, vi } from 'vitest';
import { useEffect, useRef } from 'react';
import { render } from 'vitest-browser-react';
import { GuestPoolProvider, useGuestPool, type GuestPoolAPI } from './GuestPoolContext';
import { OVERLAY_RULER_PX, snapToDevice } from '@/lib/guestPoolGeometry';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';

const VERIFY_MS = 500;

function nextFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

async function waitFor(predicate: () => boolean, timeoutMs: number, label: string): Promise<void> {
  const start = performance.now();
  for (;;) {
    if (predicate()) return;
    if (performance.now() - start > timeoutMs) throw new Error(`timed out waiting for ${label}`);
    await nextFrame();
  }
}

/** The x of the element's `translate(...)`, as the sync pass wrote it. */
function elementX(el: HTMLElement): number {
  const m = /translate\((-?[\d.]+)px/.exec(el.style.transform);
  return m ? Number(m[1]) : NaN;
}

/** Where the pass ought to have put it, from live layout: over the
 *  container, on whole device pixels (no camera origin here, so the
 *  viewport's grid). */
function containerX(container: HTMLElement, overlay: HTMLElement): number {
  return snapToDevice(container.getBoundingClientRect().left, window.devicePixelRatio) - overlay.getBoundingClientRect().left;
}

interface Harness {
  overlay: HTMLElement;
  container: HTMLElement;
  element: HTMLElement;
  pool: GuestPoolAPI;
}

/** Optional card dressing: the wrapper as a rounded, bordered stack card
 *  and the container moved down inside it (its bottom on the card's). */
interface SceneLayout {
  card?: boolean;
  containerTop?: number;
}

function Scene({ onReady, layout = {} }: { onReady: (h: Harness) => void; layout?: SceneLayout }) {
  const pool = useGuestPool();
  const overlayRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const overlay = overlayRef.current!;
    const container = containerRef.current!;
    pool.registerOverlay(overlay);
    // about:blank rather than /sandbox.html — the pool's behaviour is the
    // subject, not the renderer's.
    pool.acquire('tracked', 'about:blank', container);
    onReady({ overlay, container, element: pool.getElement('tracked')!, pool });
  }, [pool, onReady]);

  return (
    // The overlay carries the scene's ruler width, so the pass's scale
    // (its on-screen width over OVERLAY_RULER_PX) resolves to exactly 1.
    <div
      data-stack-card={layout.card ? '' : undefined}
      style={{
        position: 'relative', width: 800, height: 400,
        ...(layout.card ? { borderRadius: 10, borderTop: '1px solid' } : {}),
      }}
    >
      <div ref={overlayRef} style={{ position: 'absolute', top: 0, left: 0, width: OVERLAY_RULER_PX, height: 0 }} />
      <div
        ref={containerRef}
        style={{ position: 'absolute', top: layout.containerTop ?? 0, left: 0, width: 200, height: 100 }}
      />
    </div>
  );
}

async function mount(layout?: SceneLayout): Promise<Harness> {
  let harness: Harness | null = null;
  await render(
    <GuestPoolProvider>
      <Scene onReady={(h) => { harness = h; }} layout={layout} />
    </GuestPoolProvider>,
  );
  await waitFor(() => harness !== null, 2000, 'the pool to hand back its element');
  return harness!;
}

/** The provider's dev-only debug handle — the only view a test has of
 *  the scheduler's internals, and the reason it exists. */
function sync() {
  const handle = window.__collimateGuestSync;
  if (!handle) throw new Error('__collimateGuestSync missing — dev build only');
  return handle;
}

describe('guest-pool sync loop (browser mode)', () => {
  it('tracks a moving container, then parks once it stops', async () => {
    const { overlay, container, element } = await mount();

    await waitFor(() => Math.abs(elementX(element) - containerX(container, overlay)) < 1, 1000,
      'the first pass to position the element');

    const animation = container.animate(
      [{ transform: 'translateX(0px)' }, { transform: 'translateX(400px)' }],
      { duration: 400, fill: 'forwards' },
    );

    // Mid-flight: the element has left the origin and is within one
    // frame's travel (~16px at this speed) of its container. Nothing
    // told the pool about this animation — if the loop had parked, the
    // element would still be sitting at 0.
    await new Promise((r) => setTimeout(r, 200));
    const midElement = elementX(element);
    const midContainer = containerX(container, overlay);
    expect(midElement).toBeGreaterThan(0);
    expect(Math.abs(midElement - midContainer)).toBeLessThan(40);

    await animation.finished;
    await waitFor(() => Math.abs(elementX(element) - containerX(container, overlay)) < 1, 1000,
      'the element to land on the animation\'s final rect');

    // …and now the half that this whole change is for.
    await waitFor(() => sync().state() === 'idle', 2000, 'the loop to park');
    expect(sync().stats().missedWakes).toBe(0);
  });

  it('catches a move with no wake source at all, on the verify tick', async () => {
    const { overlay, container, element } = await mount();
    await waitFor(() => sync().state() === 'idle', 2000, 'the loop to park');
    const before = sync().stats().missedWakes;

    // A layout change through a channel the pool has no hook into: no
    // event, no React commit, no camera motion, no pool call. Exactly
    // the shape of a wake source we forgot to wire.
    container.style.left = '500px';

    await waitFor(
      () => Math.abs(elementX(element) - containerX(container, overlay)) < 1,
      VERIFY_MS * 3,   // 2x the period, plus slack for a loaded CI machine
      'the verify tick to notice and correct the position',
    );
    // And it reported itself rather than quietly papering over the gap.
    expect(sync().stats().missedWakes).toBe(before + 1);
  });

  it('still parks on fractional rects, and the verify tick puts back a displaced element', async () => {
    const { overlay, container, element } = await mount();
    // Fractional coordinates: CSSOM re-serializes them on read-back
    // (Chrome keeps six significant digits), so this is the case where a
    // naive "does the element hold what we wrote?" string compare would
    // read dirty forever and pin the loop awake.
    container.style.left = '123.456789px';
    container.style.top = '7.3333333px';
    // The frame snaps to device pixels; an overlay off that grid keeps the
    // translate it is written fractional.
    overlay.style.left = '0.371px';
    window.dispatchEvent(new Event('resize'));   // a real wake source
    await waitFor(() => Math.abs(elementX(element) - containerX(container, overlay)) < 0.01, 1000,
      'the element to land on the fractional rect');
    await waitFor(() => sync().state() === 'idle', 2000, 'the loop to park on a fractional rect');
    const before = sync().stats().missedWakes;

    // The container has not moved; the element has — a write the pool
    // did not make. Before the fix the pass only compared the container
    // with its own cache, found them equal, and this offset (a gap on
    // the left, spilling past the cell on the right) stayed for good.
    element.style.transform = 'translate(300px, 7px)';

    await waitFor(
      () => Math.abs(elementX(element) - containerX(container, overlay)) < 0.01,
      VERIFY_MS * 3,
      'the verify tick to put the element back over its container',
    );
    expect(sync().stats().missedWakes).toBe(before + 1);
    await waitFor(() => sync().state() === 'idle', 2000, 'the loop to park again');
  });

  it('a live guest whose container meets the card\'s bottom stays LIVE (polish)', async () => {
    // The container's bottom is the card's, its top is not (a tab strip
    // above it), so its bottom corners take the card's inner radius. The
    // thaw and the sync pass must agree on that radius, or each pass reads
    // the thaw's layout as motion and freezes the live guest.
    const shellStub = installDesktopShell();
    try {
      const { container, pool } = await mount({ card: true, containerTop: 300 });
      const guestId = pool.acquireGuest('web', { kind: 'guest', create: () => 'browser-guest' }, container)!;
      pool.setGuestReady('web');
      pool.setVisible('web', true);
      const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
      await waitFor(
        () => setVisible.mock.calls.some(([id, visible]) => id === guestId && visible === true),
        3000,
        'the guest to go LIVE once the scene settles',
      );
      setVisible.mockClear();
      await new Promise((resolve) => setTimeout(resolve, 2 * VERIFY_MS + 100));
      expect(setVisible.mock.calls.filter(([id, visible]) => id === guestId && visible === false)).toEqual([]);
      const radii = shellStub.layoutBatches.flatMap(b => b.guests).filter(g => g.guestId === guestId).map(g => g.radius);
      expect(radii.length).toBeGreaterThan(0);
      expect(new Set(radii).size).toBe(1);
    } finally {
      uninstallDesktopShell();
    }
  });

  it('a guest goes LIVE at rest, streams real layout, freezes for motion, thaws after', async () => {
    // The native-view half of the pool, against the stubbed shell: the
    // placeholder tracks the container in the compositor like any pooled
    // element, while the layout batch stream carries the *window* rect —
    // the coordinate space camera motion actually changes.
    const shellStub = installDesktopShell();
    try {
      const { overlay, container, pool } = await mount();
      // Acquire through the pool exactly as a mount would.
      const guestId = pool.acquireGuest('web', { kind: 'guest', create: () => 'browser-guest' }, container)!;
      pool.setGuestReady('web');
      pool.setVisible('web', true);
      const placeholder = pool.getElement('web')!;
      const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);

      // At rest (the loop parks), the guest thaws: bounds first, then show.
      await waitFor(
        () => setVisible.mock.calls.some(([id, visible]) => id === guestId && visible === true),
        3000,
        'the guest to go LIVE once the scene settles',
      );
      const liveBatch = shellStub.layoutBatches.at(-1)!;
      const rect = container.getBoundingClientRect();
      expect(liveBatch.guests.some((g) =>
        g.guestId === guestId &&
        Math.abs(g.x - Math.floor(rect.left)) <= 1 &&
        Math.abs(g.w - Math.floor(rect.width)) <= 1,
      )).toBe(true);

      // Motion: the placeholder tracks in the compositor, the batch stream
      // follows the moving window rect, and the native view freezes.
      setVisible.mockClear();
      const batchesBefore = shellStub.layoutBatches.length;
      const animation = container.animate(
        [{ transform: 'translateX(0px)' }, { transform: 'translateX(300px)' }],
        { duration: 400, fill: 'forwards' },
      );
      await waitFor(
        () => setVisible.mock.calls.some(([id, visible]) => id === guestId && visible === false),
        2000,
        'the moving guest to freeze',
      );
      await animation.finished;
      await waitFor(() => sync().state() === 'idle', 2000, 'the loop to park after the animation');
      expect(shellStub.layoutBatches.length).toBeGreaterThan(batchesBefore);
      // The placeholder went along for the ride like any pooled element.
      await waitFor(
        () => Math.abs(elementX(placeholder) - containerX(container, overlay)) < 1,
        1000,
        'the placeholder to land on the final rect',
      );
      // …and the settle brought the view back.
      await waitFor(
        () => setVisible.mock.calls.some(([id, visible]) => id === guestId && visible === true),
        2000,
        'the guest to thaw once the scene settles again',
      );
    } finally {
      uninstallDesktopShell();
    }
  });
});
