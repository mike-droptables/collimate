/**
 * @vitest-environment jsdom
 *
 * mountGuestInto against a real pool and the desktopShell stub: minting,
 * event routing, the handle's op surface, pointer-event handling, and
 * teardown semantics. This is the successor to WebviewHost.test.tsx —
 * the component died with the `<webview>` tag; the imperative mount is
 * the whole production path now (NativeRenderer → api.desktop.mountGuest).
 */
import { useEffect } from 'react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { GuestPoolProvider, useGuestPool, type GuestPoolAPI } from '@/contexts/GuestPoolContext';
import { GUEST_FOCUS_EVENT, mountGuestInto, partitionFor, PARTITION_PREFIX, type HostGuestHandle } from './guestMount';
import {
  installDesktopShell,
  uninstallDesktopShell,
  type DesktopShellStub,
} from '@/test/desktopShell';

let pool: GuestPoolAPI;
let overlay: HTMLElement;
let shellStub: DesktopShellStub;

function Capture() {
  const api = useGuestPool();
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

function host(): HTMLElement {
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

let seq = 0;
function mount(spec: Partial<Parameters<typeof mountGuestInto>[2]> = {}): HostGuestHandle {
  return mountGuestInto(pool, spec.poolId ? host() : host(), {
    poolId: `pane-${++seq}`,
    src: 'https://example.test/',
    partition: 'preview',
    ...spec,
  });
}

beforeEach(() => {
  shellStub = installDesktopShell();
  mountPool();
});

afterEach(() => {
  cleanup();
  overlay.remove();
  uninstallDesktopShell();
});

describe('partitionFor', () => {
  it('prefixes a bare name', () => {
    expect(partitionFor('preview')).toBe(`${PARTITION_PREFIX}preview`);
  });

  it('sanitises to the character set the shell accepts', () => {
    expect(partitionFor('Cell 42/Renderer.web')).toBe(`${PARTITION_PREFIX}cell-42-renderer-web`);
  });

  it('collapses punctuation runs and trims the edges', () => {
    expect(partitionFor('--a//b--')).toBe(`${PARTITION_PREFIX}a-b`);
  });

  it('is idempotent — an already-prefixed name is not doubled', () => {
    expect(partitionFor(`${PARTITION_PREFIX}preview`)).toBe(`${PARTITION_PREFIX}preview`);
  });

  it('always leaves something after the prefix', () => {
    expect(partitionFor('///')).toBe(`${PARTITION_PREFIX}default`);
    expect(partitionFor('')).toBe(`${PARTITION_PREFIX}default`);
  });

  it('never lets an unprefixed persist: name masquerade as managed', () => {
    expect(partitionFor('persist:evil')).toBe(`${PARTITION_PREFIX}persist-evil`);
  });
});

describe('mounting', () => {
  it('creates the shell guest with a prefixed partition and the mute policy', async () => {
    const handle = mount({ partition: 'My Preview' });
    const guestId = await handle.guestId;

    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledExactlyOnceWith({
      guestId,
      partition: `${PARTITION_PREFIX}my-preview`,
      url: 'https://example.test/',
      device: 'desktop',
      // An ordinary page must not keep playing audio under its screenshot.
      muteWhenHidden: true,
    });
  });

  it('creates the guest as the device the mount names (GN-04)', async () => {
    const handle = mount({ device: 'phone' });
    await handle.guestId;
    expect(vi.mocked(shellStub.shell.guest.createGuest).mock.calls.at(-1)![0]).toMatchObject({ device: 'phone' });
  });

  it('keepAlive guests keep their audio while hidden', async () => {
    const handle = mount({ keepAlive: true });
    await handle.guestId;
    expect(vi.mocked(shellStub.shell.guest.createGuest).mock.calls.at(-1)![0]).toMatchObject({
      muteWhenHidden: false,
    });
  });

  it('reveals the placeholder once the shell says the page is ready', async () => {
    const poolId = 'ready-pane';
    const handle = mountGuestInto(pool, host(), {
      poolId,
      src: 'https://example.test/',
      partition: 'preview',
    });
    const guestId = await handle.guestId;
    const element = pool.getElement(poolId)!;
    expect(element.style.visibility).toBe('hidden');

    shellStub.emitGuestEvent({ guestId, kind: 'ready', data: { url: 'https://example.test/' } });

    expect(element.style.visibility).toBe('visible');
  });

  it('shares a pool entry across re-mounts of the same poolId', async () => {
    const poolId = 'shared-pane';
    const first = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'preview',
    });
    const firstId = await first.guestId;
    first.destroy();

    const second = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'preview',
    });
    // The parked guest is still there: same id, no second createGuest.
    expect(await second.guestId).toBe(firstId);
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(1);
  });
});

describe('device (GN-04)', () => {
  it('setDevice switches its own guest in place: no new guest, no remount (GN-04)', async () => {
    const handle = mount({ keepAlive: true, guestSessionKey: 'run-1' });
    const guestId = await handle.guestId;
    await handle.setDevice('tablet');
    expect(shellStub.shell.guest.setGuestDevice).toHaveBeenCalledExactlyOnceWith(guestId, 'tablet');
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(1);
    expect(shellStub.shell.guest.destroyGuest).not.toHaveBeenCalled();
  });

  it('a reattached guest built for another device is switched; the same device is left alone (GN-04)', async () => {
    const spec = { poolId: 'device-pane', src: 'https://example.test/', partition: 'p', keepAlive: true };
    const first = mountGuestInto(pool, host(), { ...spec, device: 'phone' });
    const guestId = await first.guestId;
    first.destroy();
    const second = mountGuestInto(pool, host(), { ...spec, device: 'phone' });
    expect(await second.guestId).toBe(guestId);
    expect(shellStub.shell.guest.setGuestDevice).not.toHaveBeenCalled();
    second.destroy();
    const third = mountGuestInto(pool, host(), { ...spec, device: 'desktop' });
    expect(await third.guestId).toBe(guestId);
    expect(shellStub.shell.guest.setGuestDevice).toHaveBeenCalledExactlyOnceWith(guestId, 'desktop');
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(1);
  });
});

describe('session scoping', () => {
  // A pool id is stable by construction (cell + renderer + mount order), so
  // it cannot tell a remount of one session from the start of the next. The
  // session key is what does — and the case that needs it is a keepAlive
  // guest, the only kind nothing else ever reclaims.
  const spec = (guestSessionKey?: string) => ({
    poolId: 'session-pane',
    src: 'https://example.test/',
    partition: 'p',
    keepAlive: true,
    ...(guestSessionKey !== undefined ? { guestSessionKey } : {}),
  });

  it('reattaches the parked guest when the key is the same', async () => {
    const first = mountGuestInto(pool, host(), spec('run-1'));
    const guestId = await first.guestId;
    first.destroy();

    const second = mountGuestInto(pool, host(), spec('run-1'));

    expect(await second.guestId).toBe(guestId);
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(1);
    expect(shellStub.shell.guest.destroyGuest).not.toHaveBeenCalled();
  });

  it('destroys the parked guest and creates a fresh one when the key differs __F0287', async () => {
    const first = mountGuestInto(pool, host(), { ...spec('run-1'), src: 'https://old.test/' });
    const firstId = await first.guestId;
    first.destroy();

    const second = mountGuestInto(pool, host(), { ...spec('run-2'), src: 'https://new.test/' });
    const secondId = await second.guestId;

    expect(secondId).not.toBe(firstId);
    // Actually destroyed, not merely orphaned: nothing else reclaims a
    // keepAlive guest, so a leak here would last until the window reloads.
    expect(shellStub.shell.guest.destroyGuest).toHaveBeenCalledWith(firstId);
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(2);
    // And the new mount's own src is what got loaded.
    expect(vi.mocked(shellStub.shell.guest.createGuest).mock.calls.at(-1)![0]).toMatchObject({
      guestId: secondId,
      url: 'https://new.test/',
    });
  });

  it('replaces a still-live session too, not just a parked one', async () => {
    // No destroy() in between: the new session's mount lands while the old
    // one is still attached. The key decides here as well.
    const first = mountGuestInto(pool, host(), spec('run-1'));
    const firstId = await first.guestId;

    const second = mountGuestInto(pool, host(), spec('run-2'));

    expect(await second.guestId).not.toBe(firstId);
    expect(shellStub.shell.guest.destroyGuest).toHaveBeenCalledWith(firstId);
  });

  it('omitting the key keeps the old reattach-always behaviour', async () => {
    const first = mountGuestInto(pool, host(), spec());
    const guestId = await first.guestId;
    first.destroy();

    const second = mountGuestInto(pool, host(), spec());

    expect(await second.guestId).toBe(guestId);
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(1);
  });
});

describe('the handle', () => {
  it('drives the shell ops against its own guest id', async () => {
    const handle = mount();
    const guestId = await handle.guestId;
    const guest = shellStub.shell.guest;

    await handle.loadURL('https://other.test/');
    expect(guest.navigate).toHaveBeenCalledWith(guestId, 'https://other.test/');
    await handle.reload();
    expect(guest.reload).toHaveBeenCalledWith(guestId);
    await handle.evaluate('1 + 1');
    expect(guest.evaluate).toHaveBeenCalledWith(guestId, '1 + 1');
    expect(await handle.goBack()).toBe(true);
    expect(await handle.goForward()).toBe(false);
    await handle.currentUrl();
    expect(guest.currentUrl).toHaveBeenCalledWith(guestId);
    await handle.capturePng();
    expect(guest.capturePng).toHaveBeenCalledWith(guestId);
  });

  it('routes only its own guest events', async () => {
    const handle = mount();
    const guestId = await handle.guestId;
    const titles: string[] = [];
    handle.on('title', ({ title }) => titles.push(title));

    shellStub.emitGuestEvent({ guestId: 'someone-else', kind: 'title', data: { title: 'not ours' } });
    shellStub.emitGuestEvent({ guestId, kind: 'title', data: { title: 'ours' } });

    expect(titles).toEqual(['ours']);
  });

  it('delivers the new event kinds — focus and ready — to subscribers', async () => {
    const handle = mount();
    const guestId = await handle.guestId;
    const seen: string[] = [];
    handle.on('focus', ({ focused }) => seen.push(`focus:${focused}`));
    handle.on('ready', ({ url }) => seen.push(`ready:${url}`));

    shellStub.emitGuestEvent({ guestId, kind: 'focus', data: { focused: true } });
    shellStub.emitGuestEvent({ guestId, kind: 'ready', data: { url: 'https://example.test/' } });

    expect(seen).toEqual(['focus:true', 'ready:https://example.test/']);
  });

  it('re-emits a focusing wheel on the host, composed out of a shadow root, for the cell to select on', async () => {
    const shadowHost = host();
    const hostEl = document.createElement('div');
    shadowHost.attachShadow({ mode: 'open' }).append(hostEl);
    const handle = mountGuestInto(pool, hostEl, { poolId: 'wheel-pane', src: 'https://example.test/', partition: 'p' });
    const guestId = await handle.guestId;
    const wheels: WheelEvent[] = [];
    // Outside the shadow root, as the card and the leaf are.
    shadowHost.parentElement!.addEventListener('wheel', (e) => wheels.push(e as WheelEvent));

    shellStub.emitGuestEvent({ guestId: 'someone-else', kind: 'wheel', data: { deltaX: 0, deltaY: 40, ctrlKey: false } });
    expect(wheels).toHaveLength(0);
    shellStub.emitGuestEvent({ guestId, kind: 'wheel', data: { deltaX: 2, deltaY: 40, ctrlKey: false } });
    expect(wheels).toHaveLength(1);
    expect(wheels[0]).toMatchObject({ deltaX: 2, deltaY: 40, deltaMode: 0, bubbles: true, composed: true });
    // Sideways travel and the pinch stay the guest's own.
    shellStub.emitGuestEvent({ guestId, kind: 'wheel', data: { deltaX: 60, deltaY: 5, ctrlKey: false } });
    shellStub.emitGuestEvent({ guestId, kind: 'wheel', data: { deltaX: 0, deltaY: 40, ctrlKey: true } });
    expect(wheels).toHaveLength(1);
  });

  it('reports the guest taking focus on the host, composed out of a shadow root', async () => {
    const shadowHost = host();
    const hostEl = document.createElement('div');
    shadowHost.attachShadow({ mode: 'open' }).append(hostEl);
    const handle = mountGuestInto(pool, hostEl, { poolId: 'focus-pane', src: 'https://example.test/', partition: 'p' });
    const guestId = await handle.guestId;
    const focused = vi.fn();
    shadowHost.parentElement!.addEventListener(GUEST_FOCUS_EVENT, focused);
    shellStub.emitGuestEvent({ guestId, kind: 'focus', data: { focused: false } });
    expect(focused).not.toHaveBeenCalled();
    shellStub.emitGuestEvent({ guestId, kind: 'focus', data: { focused: true } });
    expect(focused).toHaveBeenCalledTimes(1);
  });

  it('unsubscribes a handler on demand', async () => {
    const handle = mount();
    const guestId = await handle.guestId;
    const handler = vi.fn();
    const off = handle.on('loading', handler);

    shellStub.emitGuestEvent({ guestId, kind: 'loading', data: { loading: true } });
    off();
    shellStub.emitGuestEvent({ guestId, kind: 'loading', data: { loading: false } });

    expect(handler).toHaveBeenCalledExactlyOnceWith({ loading: true });
  });
});

describe('pointer events', () => {
  it('lets a canvas pan claim the surface, then hands it back', async () => {
    const poolId = 'pan-pane';
    mountGuestInto(pool, host(), { poolId, src: 'https://example.test/', partition: 'p' });
    const element = pool.getElement(poolId)!;
    expect(element.style.pointerEvents).toBe('auto');

    window.dispatchEvent(new Event('canvas-pan-start'));
    expect(element.style.pointerEvents).toBe('none');
    window.dispatchEvent(new Event('canvas-pan-end'));
    expect(element.style.pointerEvents).toBe('auto');
  });

  it('does not hand the surface back to a guest that never wanted it', async () => {
    const poolId = 'passive-pane';
    mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'p', interactive: false,
    });
    const element = pool.getElement(poolId)!;
    expect(element.style.pointerEvents).toBe('none');

    window.dispatchEvent(new Event('canvas-pan-start'));
    window.dispatchEvent(new Event('canvas-pan-end'));
    expect(element.style.pointerEvents).toBe('none');
  });

  it('setInteractive flips both input and LIVE eligibility', async () => {
    const poolId = 'flip-pane';
    const handle = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'p',
    });
    const element = pool.getElement(poolId)!;

    handle.setInteractive(false);
    expect(element.style.pointerEvents).toBe('none');
    handle.setInteractive(true);
    expect(element.style.pointerEvents).toBe('auto');
  });
});

describe('crash recovery', () => {
  it('a crash hides the view AND the placeholder so cell-DOM crash UI can show', async () => {
    const poolId = 'crash-pane';
    const handle = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'p',
    });
    const guestId = await handle.guestId;
    const element = pool.getElement(poolId)!;
    shellStub.emitGuestEvent({ guestId, kind: 'ready', data: { url: 'https://example.test/' } });
    expect(element.style.visibility).toBe('visible');

    shellStub.emitGuestEvent({ guestId, kind: 'crashed', data: { reason: 'crashed', exitCode: 139 } });

    // The placeholder goes on the spot — the renderer's crash panel lives
    // in cell DOM the overlay would otherwise cover. (The live-view hide
    // half is asserted in the pool's crash test, where the scheduler is
    // mocked and the guest can actually be LIVE.)
    expect(element.style.visibility).toBe('hidden');

    // A reload that completes (the shell pushes `ready` again) restores.
    shellStub.emitGuestEvent({ guestId, kind: 'ready', data: { url: 'https://example.test/' } });
    expect(element.style.visibility).toBe('visible');
  });

  it('crash events still reach renderer subscribers', async () => {
    const handle = mount();
    const guestId = await handle.guestId;
    const crashes: string[] = [];
    handle.on('crashed', ({ reason }) => crashes.push(reason));

    shellStub.emitGuestEvent({ guestId, kind: 'crashed', data: { reason: 'oom', exitCode: 137 } });

    expect(crashes).toEqual(['oom']);
  });
});

describe('teardown', () => {
  it('parks an ordinary guest rather than destroying it', async () => {
    const poolId = 'parked-pane';
    const handle = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'p',
    });
    await handle.guestId;

    handle.destroy();

    // Still pooled (warm for a scroll-back), just parked and hidden.
    const element = pool.getElement(poolId)!;
    expect(element.isConnected).toBe(true);
    expect(element.style.transform).toBe('translate(-9999px, -9999px)');
    expect(shellStub.shell.guest.destroyGuest).not.toHaveBeenCalled();
  });

  it('parks a keepAlive guest too — destroy releases the mount, never the guest', async () => {
    // A mount dies on every renderer remount (scroll-back, a cell moved
    // between stacks, zoom transitions); the page — its URL, history,
    // logins — must survive all of them. That is the whole point of
    // keepAlive, so destroy() parks and the next mount reattaches.
    const poolId = 'pinned-pane';
    const handle = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'p', keepAlive: true,
    });
    const guestId = await handle.guestId;

    handle.destroy();

    expect(pool.getElement(poolId)!.isConnected).toBe(true);
    expect(shellStub.shell.guest.destroyGuest).not.toHaveBeenCalled();

    const again = mountGuestInto(pool, host(), {
      poolId, src: 'https://example.test/', partition: 'p', keepAlive: true,
    });
    expect(await again.guestId).toBe(guestId);
    expect(shellStub.shell.guest.createGuest).toHaveBeenCalledTimes(1);
  });

  it('a fresh mount can clear a conceal its dead predecessor left behind', async () => {
    // A cell dropped onto another stack remounts its renderer while
    // drag-hidden: the old mount dies with the entry concealed, the new
    // one takes the entry over. The host re-syncs conceal on every fresh
    // mount (NativeRenderer), so the flag must be clearable through the
    // successor's handle.
    const poolId = 'conceal-pane';
    const spec = { poolId, src: 'https://example.test/', partition: 'p' };
    const first = mountGuestInto(pool, host(), spec);
    const guestId = await first.guestId;
    shellStub.emitGuestEvent({ guestId, kind: 'ready', data: { url: 'https://example.test/' } });
    first.setConcealed(true);
    first.destroy();

    const second = mountGuestInto(pool, host(), spec);
    second.setConcealed(false);

    expect(pool.getElement(poolId)!.style.visibility).toBe('visible');
  });

  it('stops listening to the shell once destroyed', async () => {
    const handle = mount();
    expect(shellStub.subscriberCount()).toBe(1);
    handle.destroy();
    expect(shellStub.subscriberCount()).toBe(0);
  });

  it('is idempotent', async () => {
    const handle = mount();
    handle.destroy();
    expect(() => handle.destroy()).not.toThrow();
  });
});

describe('without a shell', () => {
  it('refuses instead of pretending, and touches nothing', async () => {
    uninstallDesktopShell();
    const acquireGuest = vi.spyOn(pool, 'acquireGuest');

    const handle = mountGuestInto(pool, host(), {
      poolId: 'browser-pane', src: 'https://example.test/', partition: 'p',
    });

    expect(acquireGuest).not.toHaveBeenCalled();
    await expect(handle.guestId).rejects.toThrow(/native guest/);
    await expect(handle.loadURL('https://x.test/')).rejects.toThrow(/native guest/);
    expect(() => handle.setInteractive(false)).not.toThrow();
    // No guest to present as anything: switching the device is a no-op (GN-04).
    await expect(handle.setDevice('phone')).resolves.toBeUndefined();
    expect(() => handle.destroy()).not.toThrow();
  });
});
