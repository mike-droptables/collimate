/**
 * Guest-pool invariants.
 *
 * The iframe half is the pool's original contract: elements created once
 * and never moved, so most of what is asserted is an absence — no second
 * appendChild, no re-created element, no detach on park (a move is a
 * reload for an iframe).
 *
 * The guest half changed shape with the WebContentsView migration: the
 * pool holds a *placeholder* div (screenshot inside) and drives the real
 * pixels through `window.collimateDesktop.guest` — created on acquire,
 * positioned by the layout batch the sync pass emits, LIVE only when the
 * scene is at rest and nothing covers it, destroyed on eviction. jsdom
 * never sees a native view, which is the point: the desktopShell stub
 * records every shell call, so the whole presentation machine is
 * assertable as data.
 */
import { useEffect } from 'react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import {
  GuestPoolProvider,
  useGuestPool,
  type GuestPoolAPI,
  type GuestSpec,
} from './GuestPoolContext';
import type { GuestSyncDeps } from '@/lib/guestSyncScheduler';
import { registerGuestOccluder, resetGuestOcclusion } from '@/lib/guestOcclusion';
import { CAMERA_ORIGIN_ATTR, OVERLAY_RULER_PX } from '@/lib/guestPoolGeometry';
import {
  installDesktopShell,
  uninstallDesktopShell,
  type DesktopShellStub,
} from '@/test/desktopShell';

// The sync loop only runs while something is moving, which makes every
// pool mutation responsible for saying so. Standing in a spy-backed fake
// for the scheduler turns "did this call wake the loop?" into a direct
// assertion, and keeps a real rAF/interval pair out of the unit suite.
// `atRest` reports true so the LIVE gate sees a still scene by default.
const guestSync = vi.hoisted(() => {
  const invalidate = vi.fn();
  const stop = vi.fn();
  let lastDeps: unknown = null;
  let restful = true;
  return {
    invalidate,
    stop,
    setAtRest: (value: boolean) => { restful = value; },
    create: vi.fn((deps: unknown) => {
      lastDeps = deps;
      return {
        invalidate,
        state: () => 'idle' as const,
        stats: () => ({ passes: 0, wakes: 0, missedWakes: 0 }),
        atRest: () => restful,
        stop,
      };
    }),
    /** The deps bag the provider handed the scheduler — `pass()` is the
     *  only way a test can run a sync pass on demand, and `onSettle()` is
     *  the "camera came to rest" edge. */
    deps: () => lastDeps as GuestSyncDeps,
  };
});

vi.mock('@/lib/guestSyncScheduler', () => ({
  createGuestSyncScheduler: guestSync.create,
  guestSyncAlwaysRun: () => false,
}));

// Mirrors SLEEPING_IFRAME_FLOOR / SLEEPING_GUEST_FLOOR in the module. Not
// imported: the numbers are the behaviour under test, and importing them
// would make the assertions agree with any value.
const IFRAME_FLOOR = 20;
const GUEST_FLOOR = 4;

let pool: GuestPoolAPI;
let overlay: HTMLElement;
let shellStub: DesktopShellStub;

function Capture() {
  const api = useGuestPool();
  // In an effect, not during render: the tests drive the pool imperatively
  // and render() has flushed effects by the time they do.
  useEffect(() => { pool = api; }, [api]);
  return null;
}

function mountPool(): void {
  render(
    <GuestPoolProvider>
      <Capture />
    </GuestPoolProvider>,
  );
  overlay = document.createElement('div');
  document.body.appendChild(overlay);
  pool.registerOverlay(overlay);
}

function container(): HTMLElement {
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

/** Give an element a controllable rect — jsdom's own is all zeroes. */
function setRect(el: HTMLElement, rect: { x: number; y: number; w: number; h: number }): void {
  el.getBoundingClientRect = () =>
    ({
      x: rect.x,
      y: rect.y,
      left: rect.x,
      top: rect.y,
      right: rect.x + rect.w,
      bottom: rect.y + rect.h,
      width: rect.w,
      height: rect.h,
      toJSON: () => ({}),
    }) as DOMRect;
}

let mintCounter = 0;
function guestSpec(overrides: Partial<Extract<GuestSpec, { kind: 'guest' }>> = {}): GuestSpec {
  return {
    kind: 'guest',
    create: () => `guest-${++mintCounter}`,
    ...overrides,
  };
}

/** Acquire + walk a guest to LIVE: created, ready, revealed, at rest. */
function liveGuest(id: string, host: HTMLElement, cellId?: string): string {
  const guestId = pool.acquireGuest(id, guestSpec(), host, cellId);
  pool.setGuestReady(id);
  pool.setVisible(id, true, host);
  return guestId!;
}

beforeEach(() => {
  // Vitest 4 exposes Node's blob URL helpers, but jsdom cannot decode those
  // URLs into images. Pixel-lifecycle tests below install controlled helpers.
  Reflect.deleteProperty(URL, 'createObjectURL');
  Reflect.deleteProperty(URL, 'revokeObjectURL');
  shellStub = installDesktopShell();
  guestSync.setAtRest(true);
  mountPool();
});

afterEach(() => {
  cleanup();
  overlay.remove();
  uninstallDesktopShell();
  resetGuestOcclusion();
});

describe('iframe entries', () => {
  it('creates one iframe per pool id, inside the overlay', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    pool.acquire('b', '/sandbox.html?b', container());

    const a = pool.getElement('a');
    expect(a?.tagName.toLowerCase()).toBe('iframe');
    expect(a?.parentElement).toBe(overlay);
    expect(a?.getAttribute('src')).toBe('/sandbox.html?a');
    expect(pool.getElement('b')).not.toBe(a);
  });

  it('keeps the same element when the same id is re-acquired', () => {
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host);
    const first = pool.getElement('a');
    pool.acquire('a', '/sandbox.html?a', host);
    expect(pool.getElement('a')).toBe(first);
  });

  it('retires a physical source on navigation before creating the next iframe', () => {
    const host = container();
    const disposed=vi.fn((target:Window)=>{expect(pool.getContentWindow('a')).toBeNull();expect(target).toBe((element as HTMLIFrameElement|null)?.contentWindow);});
    pool.acquire('a', '/sandbox.html?v=1', host,undefined,disposed);
    const element = pool.getElement('a');
    pool.acquire('a', '/sandbox.html?v=2', host);

    expect(disposed).toHaveBeenCalledOnce();
    expect(pool.getElement('a')).not.toBe(element);
    expect(pool.getElement('a')?.getAttribute('src')).toBe('/sandbox.html?v=2');
    // Ready state resets so the consumer fades in again for the new file.
    expect(pool.getElement('a')?.style.visibility).toBe('hidden');
  });

  it('keeps source provenance while parked and disposes it on overlay replacement',()=>{
    const host=container(),disposed=vi.fn();pool.acquire('a','/sandbox.html?a',host,undefined,disposed);
    const first=pool.getElement('a');pool.release('a',host);expect(disposed).not.toHaveBeenCalled();
    pool.acquire('a','/sandbox.html?a',host,undefined,disposed);expect(pool.getElement('a')).toBe(first);
    const next=document.createElement('div');document.body.append(next);pool.registerOverlay(next);
    expect(disposed).toHaveBeenCalledOnce();expect(pool.getElement('a')).not.toBe(first);
    expect(pool.getElement('a')?.parentElement).toBe(next);expect(first?.isConnected).toBe(false);
    pool.drop('a');expect(disposed).toHaveBeenCalledTimes(2);next.remove();
  });

  it('has no guest id — that is a guest-entry concept', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    expect(pool.getGuestId('a')).toBeNull();
  });
});

describe('parking', () => {
  it('hides and translates away without detaching or re-appending', () => {
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const appendChild = vi.spyOn(overlay, 'appendChild');

    pool.release('a', host);

    expect(element.parentElement).toBe(overlay);
    expect(element.style.visibility).toBe('hidden');
    expect(element.style.transform).toBe('translate(-9999px, -9999px)');
    expect(element.style.pointerEvents).toBe('none');
    expect(appendChild).not.toHaveBeenCalled();
  });

  it('un-parks the same element, still without re-appending it', () => {
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    pool.release('a', host);

    const appendChild = vi.spyOn(overlay, 'appendChild');
    pool.acquire('a', '/sandbox.html?a', container());

    expect(pool.getElement('a')).toBe(element);
    expect(appendChild).not.toHaveBeenCalled();
  });

  it('ignores a release from a consumer that no longer owns the entry', () => {
    // The fan-out toggle double-mount window: an exiting consumer's cleanup
    // fires after a new one has taken the same pool id.
    const stale = container();
    const owner = container();
    pool.acquire('a', '/sandbox.html?a', stale);
    pool.acquire('a', '/sandbox.html?a', owner);

    pool.release('a', stale);

    expect(pool.getElement('a')!.style.transform).not.toBe('translate(-9999px, -9999px)');
  });
});

describe('guest entries', () => {
  it('creates the shell guest through the spec, exactly once, behind a placeholder', () => {
    const create = vi.fn(() => 'guest-a');
    const returned = pool.acquireGuest('g', guestSpec({ create }), container());

    expect(create).toHaveBeenCalledTimes(1);
    expect(returned).toBe('guest-a');
    expect(pool.getGuestId('g')).toBe('guest-a');
    // The placeholder: a plain div with the screenshot img inside — no
    // <webview>, no element the page could reach a guest through.
    const element = pool.getElement('g')!;
    expect(element.tagName.toLowerCase()).toBe('div');
    expect(element.parentElement).toBe(overlay);
    expect(element.querySelector('img')).not.toBeNull();
  });

  it('reuses a parked entry — same placeholder, same guest, no second create', () => {
    const create = vi.fn(() => 'guest-a');
    const host = container();
    pool.acquireGuest('g', guestSpec({ create }), host);
    const element = pool.getElement('g')!;

    pool.release('g', host);
    const returned = pool.acquireGuest('g', guestSpec({ create: vi.fn(() => 'guest-b') }), container());

    expect(pool.getElement('g')).toBe(element);
    expect(create).toHaveBeenCalledTimes(1);
    expect(returned).toBe('guest-a');
  });

  it('reattaches a parked entry whose session key matches', () => {
    const host = container();
    const create = vi.fn(() => 'guest-a');
    pool.acquireGuest('g', guestSpec({ create, guestSessionKey: 'run-1' }), host);
    const element = pool.getElement('g')!;

    pool.release('g', host);
    const returned = pool.acquireGuest('g', guestSpec({ create, guestSessionKey: 'run-1' }), container());

    expect(pool.getElement('g')).toBe(element);
    expect(returned).toBe('guest-a');
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('drops a keepAlive entry outright when the acquire names another session __F0287', () => {
    // The re-run case: same cell, same renderer, same pool id — a new
    // session must not inherit the previous one's page, and the guest it
    // displaces has to actually go (LRU never reclaims a keepAlive entry).
    const host = container();
    const firstId = pool.acquireGuest('g', guestSpec({ keepAlive: true, guestSessionKey: 'run-1' }), host);
    const firstElement = pool.getElement('g')!;
    pool.release('g', host);

    const secondId = pool.acquireGuest('g', guestSpec({ keepAlive: true, guestSessionKey: 'run-2' }), container());

    expect(shellStub.shell.guest.destroyGuest).toHaveBeenCalledWith(firstId);
    expect(firstElement.isConnected).toBe(false);
    expect(pool.getElement('g')).not.toBe(firstElement);
    expect(secondId).not.toBe(firstId);
  });

  it('parking freezes the view on the spot, with nothing to capture for', () => {
    const host = container();
    const guestId = liveGuest('g', host);
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();

    pool.release('g', host);

    expect(setVisible).toHaveBeenCalledWith(guestId, false);
    expect(pool.getElement('g')!.style.visibility).toBe('hidden');
  });

  it('is re-appended freely when a different container takes it over', () => {
    // The <webview> era forbade this (a re-append destroyed the guest);
    // a placeholder div moves for free, so guests now win the overlay's
    // paint order the same way iframes do.
    const first = container();
    pool.acquireGuest('g', guestSpec(), first);
    const element = pool.getElement('g')!;

    const appendChild = vi.spyOn(overlay, 'appendChild');
    pool.acquireGuest('g', guestSpec(), container());

    expect(pool.getElement('g')).toBe(element);
    expect(appendChild).toHaveBeenCalledWith(element);
  });

  it('is dropped by destroyGuest — element gone, shell guest destroyed', () => {
    const guestId = pool.acquireGuest('g', guestSpec(), container());
    const element = pool.getElement('g')!;

    pool.destroyGuest('g');

    expect(pool.getElement('g')).toBeNull();
    expect(element.isConnected).toBe(false);
    expect(shellStub.shell.guest.destroyGuest).toHaveBeenCalledWith(guestId);
  });

  it('is not a postMessage target', () => {
    pool.acquireGuest('g', guestSpec(), container());
    // A guest is a separate process behind the shell's bridge, not a
    // same-origin window — broadcasts must skip it rather than throw.
    expect(pool.getContentWindow('g')).toBeNull();
    expect(() => pool.broadcastToCell('c', { type: 'FILES_CHANGED' })).not.toThrow();
  });
});

describe('guest presentation', () => {
  it('resnaps unchanged CSS geometry and sends live page zoom __F0985', () => {
    const host = container();
    setRect(host, { x: 10.6, y: 20.4, w: 100.6, h: 80.4 });
    const guestId = liveGuest('zoom-guest', host);
    guestSync.deps().pass();
    shellStub.layoutBatches.length = 0;
    const wakes = guestSync.invalidate.mock.calls.length;
    shellStub.setZoomFactor(1.25);
    expect(guestSync.invalidate.mock.calls.length).toBeGreaterThan(wakes);
    guestSync.deps().pass();
    expect(shellStub.layoutBatches.at(-1)).toEqual({
      zoomFactor: 1.25,
      guests: [{ guestId, x: 10.4, y: 20, w: 100, h: 80, radius: 0 }],
    });
    shellStub.layoutBatches.length = 0;
    shellStub.setZoomFactor(1);
    guestSync.deps().pass();
    expect(shellStub.layoutBatches.at(-1)?.zoomFactor).toBe(1);
  });

  it('goes LIVE only once ready, revealed, and at rest — bounds before show', () => {
    const host = container();
    setRect(host, { x: 100, y: 50, w: 640, h: 480 });
    const guestId = pool.acquireGuest('g', guestSpec(), host)!;

    // Not ready: nothing shows.
    pool.setVisible('g', true, host);
    expect(shellStub.shell.guest.setGuestVisible).not.toHaveBeenCalledWith(guestId, true);

    pool.setGuestReady('g');

    expect(shellStub.shell.guest.setGuestVisible).toHaveBeenCalledWith(guestId, true);
    // The thaw emitted the guest's window rect before showing it.
    const batch = shellStub.layoutBatches.at(-1)!;
    expect(batch.guests).toEqual([
      { guestId, x: 100, y: 50, w: 640, h: 480, radius: 0 },
    ]);
  });

  it('stays frozen while the camera is moving, thaws on the settle edge', () => {
    const host = container();
    guestSync.setAtRest(false);
    const guestId = liveGuest('g', host);
    expect(shellStub.shell.guest.setGuestVisible).not.toHaveBeenCalledWith(guestId, true);

    guestSync.setAtRest(true);
    guestSync.deps().onSettle?.();

    expect(shellStub.shell.guest.setGuestVisible).toHaveBeenCalledWith(guestId, true);
  });

  it('freezes with private JPEG placeholders when its rect moves (F1038)', async () => {
    const host = container();
    setRect(host, { x: 0, y: 0, w: 100, h: 100 });
    const guestId = liveGuest('g', host);
    const pass = guestSync.deps().pass;
    pass();
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();

    setRect(host, { x: 40, y: 0, w: 100, h: 100 });
    expect(pass()).toBe(true);

    // Deferred hide: capture first, placeholder painted, then the view
    // goes. All the test can see is that the hide lands asynchronously.
    expect(shellStub.shell.guest.capturePlaceholder).toHaveBeenCalledWith(guestId);
    expect(shellStub.shell.guest.capturePng).not.toHaveBeenCalled();
    await vi.waitFor(() => {
      expect(setVisible).toHaveBeenCalledWith(guestId, false);
    });
    // The new rect still went to the shell — a hidden view keeps fresh
    // bounds so the next thaw is show-only.
    expect(shellStub.layoutBatches.at(-1)!.guests[0]).toMatchObject({ guestId, x: 40 });
  });

  it('emits first layout and resized frozen bounds without position-only IPC', () => {
    const host = container();
    setRect(host, { x: 10, y: 20, w: 300, h: 200 });
    pool.acquireGuest('g', guestSpec(), host);
    const pass = guestSync.deps().pass;

    pass();
    const after = shellStub.layoutBatches.length;
    pass();
    expect(shellStub.layoutBatches.length).toBe(after);

    setRect(host, { x: 11, y: 20, w: 300, h: 200 });
    pass();
    expect(shellStub.layoutBatches.length).toBe(after);
    setRect(host, { x: 11, y: 20, w: 301, h: 200 });
    pass();
    expect(shellStub.layoutBatches.length).toBe(after + 1);
  });

  it('freezes under an occluder and thaws when it goes', async () => {
    const host = container();
    const guestId = liveGuest('g', host, 'cell-1');
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();

    const remove = registerGuestOccluder('all');
    await vi.waitFor(() => {
      expect(setVisible).toHaveBeenCalledWith(guestId, false);
    });

    setVisible.mockClear();
    remove();
    expect(setVisible).toHaveBeenCalledWith(guestId, true);
  });

  it('scopes cell occluders to the cells they name', async () => {
    const covered = liveGuest('a', container(), 'cell-1');
    const clear = liveGuest('b', container(), 'cell-2');
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();

    registerGuestOccluder({ cellIds: ['cell-1'] });

    await vi.waitFor(() => {
      expect(setVisible).toHaveBeenCalledWith(covered, false);
    });
    expect(setVisible).not.toHaveBeenCalledWith(clear, false);
  });

  it('an allExcept occluder spares its own cell', async () => {
    const spared = liveGuest('a', container(), 'cell-1');
    const frozen = liveGuest('b', container(), 'cell-2');
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();

    registerGuestOccluder({ allExcept: ['cell-1'] });

    await vi.waitFor(() => {
      expect(setVisible).toHaveBeenCalledWith(frozen, false);
    });
    expect(setVisible).not.toHaveBeenCalledWith(spared, false);
  });

  it('a crashed guest hides completely and stays down until it recovers', () => {
    const host = container();
    const guestId = liveGuest('g', host);
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();
    vi.mocked(shellStub.shell.guest.capturePlaceholder).mockClear();

    pool.setGuestCrashed('g', true);

    // Immediate and capture-free: a dead page's pixels are worthless, and
    // the mount's crash UI lives in cell DOM beneath the overlay.
    expect(setVisible).toHaveBeenCalledWith(guestId, false);
    expect(shellStub.shell.guest.capturePlaceholder).not.toHaveBeenCalled();
    expect(pool.getElement('g')!.style.visibility).toBe('hidden');

    // The settle edge must not resurrect it.
    setVisible.mockClear();
    guestSync.deps().onSettle?.();
    expect(setVisible).not.toHaveBeenCalledWith(guestId, true);

    pool.setGuestCrashed('g', false);
    expect(pool.getElement('g')!.style.visibility).toBe('visible');
    expect(setVisible).toHaveBeenCalledWith(guestId, true);
  });

  it('conceal hides the placeholder and the view together, capture-free', () => {
    const host = container();
    const guestId = liveGuest('g', host);
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();
    vi.mocked(shellStub.shell.guest.capturePlaceholder).mockClear();

    pool.setGuestConcealed('g', true);

    // Synchronous, no capture: the cell is mid-animation and a stale
    // frame would be worse than nothing.
    expect(setVisible).toHaveBeenCalledWith(guestId, false);
    expect(shellStub.shell.guest.capturePlaceholder).not.toHaveBeenCalled();
    expect(pool.getElement('g')!.style.visibility).toBe('hidden');

    setVisible.mockClear();
    pool.setGuestConcealed('g', false);
    expect(pool.getElement('g')!.style.visibility).toBe('visible');
    expect(setVisible).toHaveBeenCalledWith(guestId, true);
  });

  it('a non-interactive guest never goes LIVE and refreshes its screenshot instead', () => {
    vi.useFakeTimers();
    try {
      const host = container();
      pool.acquireGuest('g', guestSpec({ interactive: false }), host);
      pool.setGuestReady('g');
      pool.setVisible('g', true, host);
      const guestId = pool.getGuestId('g')!;

      expect(shellStub.shell.guest.setGuestVisible).not.toHaveBeenCalledWith(guestId, true);
      const captures = vi.mocked(shellStub.shell.guest.capturePlaceholder).mock.calls.length;
      vi.advanceTimersByTime(3100);
      expect(vi.mocked(shellStub.shell.guest.capturePlaceholder).mock.calls.length).toBeGreaterThanOrEqual(
        captures + 3,
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it('skips screenshot refreshes while the window is hidden, and repaints on return', () => {
    vi.useFakeTimers();
    try {
      const host = container();
      pool.acquireGuest('g', guestSpec({ interactive: false }), host);
      pool.setGuestReady('g');
      pool.setVisible('g', true, host);
      const capturePng = vi.mocked(shellStub.shell.guest.capturePlaceholder);

      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      capturePng.mockClear();
      vi.advanceTimersByTime(3100);
      expect(capturePng).not.toHaveBeenCalled();

      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
      expect(capturePng).toHaveBeenCalledTimes(1);
    } finally {
      Reflect.deleteProperty(document, 'visibilityState');
      vi.useRealTimers();
    }
  });

  it('a reattach takes the new mount\'s interactive flag', () => {
    vi.useFakeTimers();
    try {
      const first = container();
      const guestId = liveGuest('g', first);
      pool.release('g', first);
      const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
      setVisible.mockClear();
      const capturePng = vi.mocked(shellStub.shell.guest.capturePlaceholder);

      const second = container();
      pool.acquireGuest('g', guestSpec({ interactive: false }), second);
      pool.setVisible('g', true, second);
      guestSync.deps().onSettle?.();

      expect(setVisible).not.toHaveBeenCalledWith(guestId, true);
      capturePng.mockClear();
      vi.advanceTimersByTime(2100);
      expect(capturePng.mock.calls.length).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });

  describe('overlapping placeholder paints', () => {
    // jsdom has no object URLs, so paintPlaceholder is a no-op unless
    // these are stubbed; captures resolve only when the test says so.
    let captures: Array<(png: ArrayBuffer) => void>;
    let createObjectURL: ReturnType<typeof vi.fn>;
    let revokeObjectURL: ReturnType<typeof vi.fn>;

    beforeEach(() => {
      captures = [];
      let n = 0;
      createObjectURL = vi.fn(() => `blob:${++n}`);
      revokeObjectURL = vi.fn();
      Object.assign(URL, { createObjectURL, revokeObjectURL });
      vi.mocked(shellStub.shell.guest.capturePlaceholder).mockImplementation(
        () => new Promise<ArrayBuffer>((resolve) => { captures.push(resolve); }),
      );
    });

    afterEach(() => {
      cleanup(); // unmount revokes the captures, so before the stubs go
      Reflect.deleteProperty(URL, 'createObjectURL');
      Reflect.deleteProperty(URL, 'revokeObjectURL');
    });

    it('paints transient JPEG blobs and revokes the superseded one (F1038)', async () => {
      const host = container();
      pool.acquireGuest('g', guestSpec({ interactive: false }), host);
      pool.setGuestReady('g');      // priming capture
      pool.setVisible('g', true, host); // refresh cadence starts: a second capture
      expect(captures).toHaveLength(2);

      captures[0]!(new ArrayBuffer(8));
      await vi.waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
      captures[1]!(new ArrayBuffer(8));
      await vi.waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(2));
      expect(createObjectURL.mock.calls.map(([blob]) => (blob as Blob).type)).toEqual(['image/jpeg', 'image/jpeg']);
      expect(shellStub.shell.guest.capturePng).not.toHaveBeenCalled();

      await vi.waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:1'));
      const img = pool.getElement('g')!.querySelector('img')!;
      img.dispatchEvent(new Event('load'));
      await vi.waitFor(() => expect(img.style.visibility).toBe('visible'));
      expect(img.getAttribute('src')).toBe('blob:2');
      expect(revokeObjectURL).not.toHaveBeenCalledWith('blob:2');
    });

    it('never leave a freeze hanging with its view shown', async () => {
      const host = container();
      const guestId = liveGuest('g', host); // capture 0: the priming refresh
      const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
      setVisible.mockClear();

      window.dispatchEvent(new Event('canvas-pan-start')); // capture 1: the freeze
      expect(captures).toHaveLength(2);

      captures[1]!(new ArrayBuffer(8));
      await vi.waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
      captures[0]!(new ArrayBuffer(8)); // the refresh paint supersedes the freeze's
      await vi.waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(2));

      await vi.waitFor(() => expect(setVisible).toHaveBeenCalledWith(guestId, false));
    });
  });

  it('a pan start freezes every LIVE guest before the first moved pixel', async () => {
    const guestId = liveGuest('g', container());
    const setVisible = vi.mocked(shellStub.shell.guest.setGuestVisible);
    setVisible.mockClear();

    window.dispatchEvent(new Event('canvas-pan-start'));

    expect(shellStub.shell.guest.capturePlaceholder).toHaveBeenCalledWith(guestId);
    await vi.waitFor(() => {
      expect(setVisible).toHaveBeenCalledWith(guestId, false);
    });
  });

  it('refreshGuestLayout blanks the emitted cache so the next pass re-sends', () => {
    const host = container();
    setRect(host, { x: 10, y: 20, w: 300, h: 200 });
    pool.acquireGuest('g', guestSpec(), host);
    const pass = guestSync.deps().pass;
    pass();
    const sent = shellStub.layoutBatches.length;
    pass();
    expect(shellStub.layoutBatches.length).toBe(sent);

    pool.refreshGuestLayout('g');
    pass();
    expect(shellStub.layoutBatches.length).toBe(sent + 1);
  });
});

describe('LRU eviction', () => {
  function fillSleeping(kind: 'iframe' | 'guest', count: number, prefix = kind): void {
    for (let i = 0; i < count; i++) {
      const host = container();
      const id = `${prefix}${i}`;
      if (kind === 'iframe') pool.acquire(id, `/sandbox.html?${i}`, host);
      else pool.acquireGuest(id, guestSpec(), host);
      pool.release(id, host);
    }
  }

  it('evicts a guest once the small guest floor is full, shell guest included', () => {
    fillSleeping('guest', GUEST_FLOOR);
    const evictedGuestId = pool.getGuestId('guest0');
    expect(pool.getElement('guest0')).not.toBeNull();

    pool.acquireGuest('extra', guestSpec(), container());

    expect(pool.getElement('guest0')).toBeNull();
    expect(pool.getElement('guest1')).not.toBeNull();
    expect(shellStub.shell.guest.destroyGuest).toHaveBeenCalledWith(evictedGuestId);
  });

  it('keeps that many iframes sleeping without blinking', () => {
    // The same count that evicts a guest is nowhere near the iframe floor:
    // an iframe is a document, a guest is an OS process.
    fillSleeping('iframe', GUEST_FLOOR + 1);
    for (let i = 0; i < GUEST_FLOOR + 1; i++) {
      expect(pool.getElement(`iframe${i}`)).not.toBeNull();
    }
  });

  it('counts the two kinds separately', () => {
    // Iframe pressure must not reclaim a guest, and a full guest pool must
    // not reclaim iframes.
    fillSleeping('guest', GUEST_FLOOR - 1);
    fillSleeping('iframe', IFRAME_FLOOR);

    pool.acquire('iframe-extra', '/sandbox.html?extra', container());

    expect(pool.getElement('guest0')).not.toBeNull();
    expect(pool.getElement('iframe0')).toBeNull();
  });

  it('never evicts a keepAlive guest, however full the pool gets', () => {
    const host = container();
    pool.acquireGuest('pinned', guestSpec({ keepAlive: true }), host);
    const pinned = pool.getElement('pinned')!;
    pool.release('pinned', host);

    fillSleeping('guest', GUEST_FLOOR);
    pool.acquireGuest('extra', guestSpec(), container());

    // Parked, still loaded, still in the overlay — and the eviction landed
    // on the oldest evictable entry instead.
    expect(pool.getElement('pinned')).toBe(pinned);
    expect(pinned.isConnected).toBe(true);
    expect(pool.getElement('guest0')).toBeNull();
  });

  it('the provider unmounting drops every entry, shell guests included', () => {
    const guestId = pool.acquireGuest('g', guestSpec({ keepAlive: true }), container());
    const element = pool.getElement('g')!;

    cleanup();

    expect(shellStub.shell.guest.destroyGuest).toHaveBeenCalledWith(guestId);
    expect(element.isConnected).toBe(false);
  });

  it('reclaims a keepAlive guest only when asked to', () => {
    pool.acquireGuest('pinned', guestSpec({ keepAlive: true }), container());
    const pinned = pool.getElement('pinned')!;

    pool.destroyGuest('pinned');

    expect(pool.getElement('pinned')).toBeNull();
    expect(pinned.isConnected).toBe(false);
  });
});

describe('z-index bumping', () => {
  it('lifts the entries of the named cells, whatever kind they are', () => {
    pool.acquire('a', '/sandbox.html?a', container(), 'cell-1');
    pool.acquireGuest('g', guestSpec(), container(), 'cell-2');

    pool.bringCellsToFront(new Set(['cell-2']));
    expect(pool.getElement('a')!.style.zIndex).toBe('');
    expect(pool.getElement('g')!.style.zIndex).toBe('1');

    pool.bringCellsToFront(new Set());
    expect(pool.getElement('g')!.style.zIndex).toBe('');
  });

  it('raises a LIVE guest in the native view order too', () => {
    const guestId = liveGuest('g', container(), 'cell-1');

    pool.bringCellsToFront(new Set(['cell-1']));

    expect(shellStub.shell.guest.raiseGuest).toHaveBeenCalledWith(guestId);
  });
});

describe('overlay re-registration', () => {
  it('renews iframe provenance while carrying native placeholders over', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    pool.acquireGuest('g', guestSpec(), container());
    const iframe = pool.getElement('a')!;
    const placeholder = pool.getElement('g')!;

    const next = document.createElement('div');
    document.body.appendChild(next);
    pool.registerOverlay(next);

    expect(pool.getElement('a')).not.toBe(iframe);
    expect(iframe.isConnected).toBe(false);
    expect(pool.getElement('a')?.parentElement).toBe(next);
    // The <webview> era dropped guests here (a re-parent destroyed the
    // native element); the placeholder just moves.
    expect(pool.getElement('g')).toBe(placeholder);
    expect(placeholder.parentElement).toBe(next);

    next.remove();
  });
});

describe('sync invalidation', () => {
  // The outer beforeEach mounts and registers an overlay, both of which
  // wake the loop legitimately. Start each case from a clean slate.
  beforeEach(() => {
    guestSync.invalidate.mockClear();
  });

  it('wakes on mount, before any child has had a chance to acquire', () => {
    // Child effects run before the provider's, so an already-mounted
    // SandboxIframe will have acquired into a scheduler that did not
    // exist yet. The provider starts the loop awake to cover that.
    expect(guestSync.create).toHaveBeenCalled();
    expect(guestSync.deps().pass).toBeTypeOf('function');
  });

  it('wakes when a new overlay is registered', () => {
    const next = document.createElement('div');
    document.body.appendChild(next);

    pool.registerOverlay(next);

    expect(guestSync.invalidate).toHaveBeenCalled();
    next.remove();
  });

  it('wakes on acquire, for both kinds', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    expect(guestSync.invalidate).toHaveBeenCalled();

    guestSync.invalidate.mockClear();
    pool.acquireGuest('g', guestSpec(), container());
    expect(guestSync.invalidate).toHaveBeenCalled();
  });

  it('wakes on a re-acquire onto the same container', () => {
    // The early-return path — it repositions, so it has to wake too.
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host);
    guestSync.invalidate.mockClear();

    pool.acquire('a', '/sandbox.html?a', host);

    expect(guestSync.invalidate).toHaveBeenCalled();
  });

  it('wakes on release, but not on a release it ignores', () => {
    const stale = container();
    const owner = container();
    pool.acquire('a', '/sandbox.html?a', stale);
    pool.acquire('a', '/sandbox.html?a', owner);
    guestSync.invalidate.mockClear();

    pool.release('a', stale);       // not the owner — a no-op
    expect(guestSync.invalidate).not.toHaveBeenCalled();

    pool.release('a', owner);
    expect(guestSync.invalidate).toHaveBeenCalled();
  });

  it('wakes on destroyGuest, a re-acquire, setVisible and the z bump', () => {
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host, 'cell-1');

    for (const call of [
      () => pool.acquire('a', '/sandbox.html?a', host, 'cell-1'),
      () => pool.setVisible('a', true, host),
      () => pool.bringCellsToFront(new Set(['cell-1'])),
      () => pool.destroyGuest('a'),
    ]) {
      guestSync.invalidate.mockClear();
      call();
      expect(guestSync.invalidate).toHaveBeenCalled();
    }
  });

  it('blanks the rect cache on release, so a re-acquired entry is re-written', () => {
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const pass = guestSync.deps().pass;

    // acquire landed the rect through the cache, so the pass finds the
    // scene already quiet.
    expect(pass()).toBe(false);

    pool.release('a', host);
    pool.acquire('a', '/sandbox.html?a', host);

    // parkOffscreen moved the element while the cache still claimed the
    // old transform. Put the lie back on the element and check the pass
    // corrects it — if release() had left the cache standing, this rect
    // (identical to the pre-release one) would read as clean and the
    // element would stay parked.
    element.style.transform = 'translate(-9999px, -9999px)';
    expect(pass()).toBe(true);
    expect(element.style.transform).toBe('translate(0px, 0px)');
  });

  it('F0723 both repositions and wakes when a container resizes', () => {
    // jsdom's ResizeObserver (src/test/setup.ts) is an inert stub, so the
    // callback is captured and fired by hand.
    const original = globalThis.ResizeObserver;
    const callbacks: ResizeObserverCallback[] = [];
    class CapturingRO {
      constructor(cb: ResizeObserverCallback) { callbacks.push(cb); }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = CapturingRO as unknown as typeof ResizeObserver;
    try {
      pool.acquire('a', '/sandbox.html?a', container());
      const element = pool.getElement('a')!;
      guestSync.invalidate.mockClear();
      element.style.transform = 'translate(-9999px, -9999px)';

      callbacks[0]!([], {} as ResizeObserver);

      // Synchronous positionOver — a discrete resize must not cost a
      // frame waiting for the loop to come round.
      expect(element.style.transform).toBe('translate(0px, 0px)');
      // …and the loop is woken anyway, for the resizes that are the first
      // frame of a continuing animation.
      expect(guestSync.invalidate).toHaveBeenCalled();
    } finally {
      globalThis.ResizeObserver = original;
    }
  });
});

describe('motion layers', () => {
  // A promoted layer keeps the raster it was promoted with; under it a
  // renderer's text resamples blurry after a zoom spring. So every pooled
  // surface and every registered layer is promoted only from the first
  // moved frame to the sync loop's settle edge, and carries nothing at rest.
  const promoted = (el: HTMLElement) =>
    el.classList.contains('will-change-transform') && el.classList.contains('transform-gpu');
  const atRest = (el: HTMLElement) =>
    !el.classList.contains('will-change-transform') && !el.classList.contains('transform-gpu');

  beforeEach(() => {
    guestSync.invalidate.mockClear();
  });

  it('carry no promotion at rest: neither a pooled surface nor a registered layer', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    pool.acquireGuest('g', guestSpec(), container());
    const wrapper = document.createElement('div');
    pool.registerMotionLayer(wrapper);
    expect(atRest(pool.getElement('a')!)).toBe(true);
    expect(atRest(pool.getElement('g')!)).toBe(true);
    expect(atRest(wrapper)).toBe(true);
  });

  it('camera motion promotes every one of them and wakes the loop; the settle edge takes the classes off again', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    pool.acquireGuest('g', guestSpec(), container());
    const wrapper = document.createElement('div');
    wrapper.className = 'absolute inset-0';
    pool.registerMotionLayer(wrapper);
    guestSync.invalidate.mockClear();

    pool.markMoving();
    expect(promoted(pool.getElement('a')!)).toBe(true);
    expect(promoted(pool.getElement('g')!)).toBe(true);
    expect(promoted(wrapper)).toBe(true);
    expect(guestSync.invalidate).toHaveBeenCalled();

    // Frames of the same motion keep them promoted.
    pool.markMoving();
    expect(promoted(wrapper)).toBe(true);

    guestSync.deps().onSettle!();
    expect(atRest(pool.getElement('a')!)).toBe(true);
    expect(atRest(pool.getElement('g')!)).toBe(true);
    expect(wrapper.className).toBe('absolute inset-0');
  });

  it('a pass that moved a surface promotes before its frame paints', () => {
    const host = container();
    setRect(host, { x: 40, y: 177, w: 472, h: 709 });
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const pass = guestSync.deps().pass;
    pass();
    guestSync.deps().onSettle!();
    expect(atRest(element)).toBe(true);

    // A clean pass is no motion.
    expect(pass()).toBe(false);
    expect(atRest(element)).toBe(true);

    // A stack spring moves the container: the pass that writes the new
    // rect promotes in the same frame.
    setRect(host, { x: 52, y: 177, w: 472, h: 709 });
    expect(pass()).toBe(true);
    expect(promoted(element)).toBe(true);
    guestSync.deps().onSettle!();
    expect(atRest(element)).toBe(true);
  });

  it('a pass that changes a surface where it stands re-writes it without promoting', () => {
    // Promoting for a write that moves nothing flipped the canvas's text
    // to the layer's raster and back at the settle edge: every glyph
    // dropped a device pixel and lost its subpixel antialiasing for the
    // ~200 ms in between.
    const host = container();
    setRect(host, { x: 40, y: 177, w: 472, h: 709 });
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const pass = guestSync.deps().pass;
    pass();
    guestSync.deps().onSettle!();

    // A resize in place (a width drag's last frame, a pane opening) and a
    // fresh surface's first placement are no motion.
    setRect(host, { x: 40, y: 177, w: 480, h: 700 });
    expect(pass()).toBe(true);
    pool.acquire('b', '/sandbox.html?b', container());
    pass();
    expect(atRest(element)).toBe(true);
    expect(atRest(pool.getElement('b')!)).toBe(true);
  });

  it('a native guest promotes when it moves on screen, not when it is resized in place', () => {
    const host = container();
    setRect(host, { x: 10, y: 20, w: 300, h: 200 });
    pool.acquireGuest('g', guestSpec(), host);
    const element = pool.getElement('g')!;
    const pass = guestSync.deps().pass;
    pass();
    guestSync.deps().onSettle!();
    expect(atRest(element)).toBe(true);

    setRect(host, { x: 10, y: 20, w: 300, h: 164 });
    expect(pass()).toBe(true);
    expect(atRest(element)).toBe(true);

    setRect(host, { x: 10, y: 32, w: 300, h: 164 });
    expect(pass()).toBe(true);
    expect(promoted(element)).toBe(true);
  });

  it('a pan gesture promotes ahead of its first moved frame', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    window.dispatchEvent(new Event('canvas-pan-start'));
    expect(promoted(pool.getElement('a')!)).toBe(true);
  });

  it('an unregistered layer and a dropped surface are left at rest', () => {
    const host = container();
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const wrapper = document.createElement('div');
    const unregister = pool.registerMotionLayer(wrapper);
    pool.markMoving();
    unregister();
    pool.drop('a', host);
    expect(atRest(wrapper)).toBe(true);
    expect(atRest(element)).toBe(true);
    guestSync.deps().onSettle!();
    pool.markMoving();
    expect(atRest(wrapper)).toBe(true);
    expect(atRest(element)).toBe(true);
  });

  it('a layer registered mid-motion joins it; a frame recreated by overlay re-registration is tracked', () => {
    pool.acquire('a', '/sandbox.html?a', container());
    pool.markMoving();
    const late = document.createElement('div');
    pool.registerMotionLayer(late);
    expect(promoted(late)).toBe(true);

    const next = document.createElement('div');
    document.body.appendChild(next);
    pool.registerOverlay(next);
    const recreated = pool.getElement('a')!;
    expect(promoted(recreated)).toBe(true);
    guestSync.deps().onSettle!();
    expect(atRest(recreated)).toBe(true);
    pool.markMoving();
    expect(promoted(recreated)).toBe(true);
    next.remove();
  });
});

describe('device-pixel placement', () => {
  const realDpr = window.devicePixelRatio;
  afterEach(() => {
    Object.defineProperty(window, 'devicePixelRatio', { value: realDpr, configurable: true });
  });

  it('the pass puts a pooled frame on whole device pixels from the camera\'s origin, and holds it there', () => {
    Object.defineProperty(window, 'devicePixelRatio', { value: 1.25, configurable: true });
    // The diagnosed rest: the camera's board under a 70px top bar, the
    // overlay where the pan put it, the container at 195.6, 181.6 CSS px.
    const board = document.createElement('div');
    board.setAttribute(CAMERA_ORIGIN_ATTR, '');
    document.body.appendChild(board);
    board.appendChild(overlay);
    setRect(board, { x: 0, y: 70, w: 1522.4, h: 1596.4 });
    setRect(overlay, { x: 761, y: 70, w: OVERLAY_RULER_PX, h: 0 });
    const host = container();
    setRect(host, { x: 195.6, y: 181.6, w: 320.8, h: 1473.8 });
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const pass = guestSync.deps().pass;
    pass();
    // 244.5 and 139.5 device px from the origin round to 245 and 140.
    expect(element.style.transform).toBe('translate(-565px, 112px)');
    // The right edge rounds on its own (516.4 → 645.5 → 646 device px).
    expect(parseFloat(element.style.width)).toBeCloseTo(320.8, 9);
    expect(parseFloat(element.style.height)).toBeCloseTo(1473.6, 9);
    // Snapped placement is still a stable one: the next pass finds nothing
    // to write, so the loop can settle.
    expect(pass()).toBe(false);
    board.remove();
  });
});

describe('positioning cache', () => {
  // The pass decides "moved" by comparing each container's rect with what
  // it believes the element holds. If that belief can drift from the
  // element, an iframe can sit offset from its cell — a gap on one side,
  // spilling past the card on the other — for as long as the container
  // stays put, and the verify tick (which runs this same pass) never
  // notices. These pin the belief to the element.
  const A = { x: 40, y: 177, w: 472, h: 709 };

  it('F0700 fractional overlay scale keeps card corners and heals the CSS cache echo', () => {
    // The overlay's ruler width at a camera scale of 1.25.
    setRect(overlay, { x: 100, y: 50, w: 1250, h: 0 });
    const card = document.createElement('div');
    card.dataset.stackCard = '';
    card.style.borderTopLeftRadius = '12px';
    // A border width computes to 0 without a style (as the card's `border` class sets).
    card.style.borderTop = '2px solid';
    document.body.appendChild(card);
    setRect(card, { x: 123, y: 73, w: 254, h: 129 });
    const host = container();
    card.appendChild(host);
    setRect(host, { x: 125, y: 75, w: 250, h: 125 });
    pool.acquire('fractional-corners', '/sandbox.html?geometry', host);
    const element = pool.getElement('fractional-corners')!;
    expect(element.style.transform).toBe('translate(20px, 20px)');
    expect(element.style.width).toBe('200px');
    expect(element.style.height).toBe('100px');
    expect(element.style.borderRadius).toBe('10px 10px 10px 10px');
    const pass = guestSync.deps().pass;
    pass();
    element.style.width = '37px';
    expect(pass()).toBe(true);
    expect(element.style.width).toBe('200px');
    expect(pass()).toBe(false);
  });

  it('a transient out-of-band write cannot outlive the container returning', () => {
    const host = container();
    setRect(host, A);
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const pass = guestSync.deps().pass;
    pass(); // settled: the element holds A and the pass knows it
    expect(element.style.transform).toBe('translate(40px, 177px)');

    // An out-of-band positioning (acquire, the ResizeObserver, a re-acquire)
    // samples the container at an instant no pass ever sees — mid-commit,
    // before a layout animation's first frame — and the container is back
    // where it was by the time the loop runs.
    setRect(host, { ...A, x: 224, w: 288 });
    pool.acquire('a', '/sandbox.html?a', host);
    expect(element.style.transform).toBe('translate(224px, 177px)');
    setRect(host, A);

    // The rect equals the one the last pass wrote, but the element does
    // not hold it any more: that is work, not a quiet frame.
    expect(pass()).toBe(true);
    expect(element.style.transform).toBe('translate(40px, 177px)');
    expect(element.style.width).toBe('472px');
    expect(pass()).toBe(false);
  });

  it('puts back an element displaced behind the pool\'s back', () => {
    const host = container();
    setRect(host, A);
    pool.acquire('a', '/sandbox.html?a', host);
    const element = pool.getElement('a')!;
    const pass = guestSync.deps().pass;
    pass(); // settled: the element holds A and the pass knows it

    element.style.transform = 'translate(80px, 177px)';
    element.style.width = '400px';

    // Nothing about the container changed, so only the element itself
    // can say the pass has work — this is what lets the verify tick heal
    // an offset whatever put it there.
    expect(pass()).toBe(true);
    expect(element.style.transform).toBe('translate(40px, 177px)');
    expect(element.style.width).toBe('472px');
    expect(pass()).toBe(false);
  });

  it('stays quiet while nothing moves, so the loop can still park', () => {
    const host = container();
    setRect(host, A);
    pool.acquire('a', '/sandbox.html?a', host);
    pool.acquire('a', '/sandbox.html?a', host);
    const pass = guestSync.deps().pass;
    pass();
    for (let i = 0; i < 5; i++) expect(pass()).toBe(false);
  });
});

describe('an occluded guest placeholder is inert scenery', () => {
  it('goes click-through and dims under a cell occluder, and comes back when it lifts', async () => {
    const host = container();
    liveGuest('g', host, 'cell-1');
    pool.setPointerEvents('g', true);
    const element = pool.getElement('g')!;
    expect(element.style.pointerEvents).toBe('auto');
    expect(element.style.opacity).toBe('1');

    const remove = registerGuestOccluder({ cellIds: ['cell-1'] });
    // Synchronous: the drop zones beneath must receive the very next
    // dragover, not one a capture round-trip later.
    expect(element.style.pointerEvents).toBe('none');
    expect(element.style.opacity).toBe('0.35');

    remove();
    expect(element.style.pointerEvents).toBe('auto');
    expect(element.style.opacity).toBe('1');
  });

  it('leaves a guest of another cell interactive', () => {
    liveGuest('a', container(), 'cell-1');
    liveGuest('b', container(), 'cell-2');
    pool.setPointerEvents('a', true);
    pool.setPointerEvents('b', true);
    const remove = registerGuestOccluder({ cellIds: ['cell-1'] });
    expect(pool.getElement('a')!.style.pointerEvents).toBe('none');
    expect(pool.getElement('b')!.style.pointerEvents).toBe('auto');
    expect(pool.getElement('b')!.style.opacity).toBe('1');
    remove();
  });

  it("remembers the consumer's own pointer-events wish across an occlusion", () => {
    liveGuest('g', container(), 'cell-1');
    pool.setPointerEvents('g', false);
    const remove = registerGuestOccluder('all');
    expect(pool.getElement('g')!.style.pointerEvents).toBe('none');
    remove();
    expect(pool.getElement('g')!.style.pointerEvents).toBe('none');
    pool.setPointerEvents('g', true);
    expect(pool.getElement('g')!.style.pointerEvents).toBe('auto');
  });
});


describe('P2 guest work bounds', () => {
  it('drops an owned iframe without dropping its replacement owner__F0716', () => {
    const first = container();
    const second = container();
    pool.acquire('ephemeral', '/sandbox.html', first);
    pool.acquire('ephemeral', '/sandbox.html', second);
    pool.drop('ephemeral', first);
    expect(pool.getElement('ephemeral')).not.toBeNull();
    pool.drop('ephemeral', second);
    expect(pool.getElement('ephemeral')).toBeNull();
  });

  it('defers parked file changes and merges paths once on reattach__F0724', () => {
    const host = container();
    pool.acquire('parked', '/sandbox.html', host, 'cell');
    const target = pool.getContentWindow('parked')!;
    const post = vi.spyOn(target, 'postMessage');
    pool.release('parked', host);
    pool.broadcastToCell('cell', { type: 'FILES_CHANGED', cellId: 'cell', changedFiles: ['a', 'b'] });
    pool.broadcastToCell('cell', { type: 'FILES_CHANGED', cellId: 'cell', changedFiles: ['b', 'c'] });
    expect(post).not.toHaveBeenCalled();
    pool.acquire('parked', '/sandbox.html', host, 'cell');
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenLastCalledWith({ type: 'FILES_CHANGED', cellId: 'cell', changedFiles: ['a', 'b', 'c'] }, window.location.origin);
    pool.release('parked', host);
    pool.acquire('parked', '/sandbox.html', host, 'cell');
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('skips frozen position layouts but sends first size and thaw geometry__F0719', () => {
    const host = container();
    guestSync.setAtRest(false);
    setRect(host, { x: 0, y: 0, w: 100, h: 100 });
    liveGuest('frozen', host);
    const pass = guestSync.deps().pass;
    pass();
    shellStub.layoutBatches.length = 0;
    setRect(host, { x: 20, y: 30, w: 100, h: 100 });
    expect(pass()).toBe(true);
    expect(shellStub.layoutBatches).toHaveLength(0);
    expect(pass()).toBe(false);
    setRect(host, { x: 20, y: 30, w: 150, h: 100 });
    pass();
    expect(shellStub.layoutBatches).toHaveLength(1);
    guestSync.setAtRest(true);
    guestSync.deps().onSettle?.();
    expect(shellStub.layoutBatches.at(-1)!.guests[0]).toMatchObject({ x: 20, y: 30, w: 150, h: 100 });
  });

  it('backs unchanged captures off and returns to fast refresh on change__F0721', async () => {
    vi.useFakeTimers();
    try {
      const capture = vi.mocked(shellStub.shell.guest.capturePlaceholder);
      let bytes = [1, 2, 3];
      capture.mockImplementation(async () => new Uint8Array(bytes).buffer);
      const host = container();
      pool.acquireGuest('passive', guestSpec({ interactive: false }), host);
      pool.setGuestReady('passive');
      pool.setVisible('passive', true, host);
      await vi.advanceTimersByTimeAsync(0);
      capture.mockClear();
      await vi.advanceTimersByTimeAsync(12_000);
      expect(capture.mock.calls.length).toBeLessThanOrEqual(4);
      bytes = [3, 2, 1];
      capture.mockClear();
      for (let i = 0; i < 5 && capture.mock.calls.length === 0; i++) await vi.advanceTimersByTimeAsync(1000);
      expect(capture).toHaveBeenCalledTimes(1);
      capture.mockClear();
      await vi.advanceTimersByTimeAsync(1000);
      expect(capture).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
