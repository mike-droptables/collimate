/**
 * mountGuestInto — the imperative half of hosting a native guest.
 *
 * Everything a mounted guest needs that isn't React lives here: minting
 * the renderer-side guest id, asking the shell to create the view
 * (`createGuest` — the page cannot construct a guest any other way),
 * taking a pool entry for the placeholder, and wiring the shell's
 * guest-event stream to per-kind subscribers. `api.desktop.mountGuest`
 * hands this straight to renderer modules, which are plain DOM code and
 * have no React to mount into.
 *
 * The pool owns the placeholder's position, visibility, presentation
 * state (LIVE native view vs FROZEN screenshot) and lifetime; this owns
 * the conversation with the shell.
 */

import {
  desktopBridge,
  hasCap,
  type GuestEvent,
  type GuestEventData,
  type GuestDevice,
  type GuestEventKind,
  type GuestHandle,
} from '@/lib/desktop';
import type { GuestPoolAPI } from '@/contexts/GuestPoolContext';
import { isFocusWheel } from '@/lib/wheelReplay';

/**
 * The only partition prefix the shell will manage.
 *
 * Mirrors `PARTITION_PREFIX` in desktop/src/shared/bridge.ts. A
 * `createGuest` whose partition isn't under it is refused, so every name
 * that reaches the shell goes through {@link partitionFor} first.
 */
export const PARTITION_PREFIX = 'persist:collimate-';

/**
 * A safe, prefixed partition string for a caller-chosen name.
 *
 * Sanitises to `[a-z0-9-]` so a name assembled from ids and file paths
 * can't smuggle in something the shell's `isManagedPartition` check would
 * read differently, and is idempotent so a caller that passes an
 * already-prefixed string gets it back unchanged rather than doubled.
 *
 * Sanitisation is lossy — `a/b` and `a b` both become `a-b`, and two
 * renderers that pick colliding names share cookies and storage. Choosing
 * distinct names is the caller's job; this only guarantees the result is
 * a partition the shell will accept.
 */
export function partitionFor(name: string): string {
  const bare = name.startsWith(PARTITION_PREFIX) ? name.slice(PARTITION_PREFIX.length) : name;
  const slug = bare.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  // The shell requires something after the prefix, so an empty slug —
  // a name that was entirely punctuation — has to become a real one.
  return `${PARTITION_PREFIX}${slug || 'default'}`;
}

/** DOM event a mount fires on its host element (bubbling, and composed out
 *  of the renderer's shadow root) when its native view takes focus. The DOM
 *  cannot see a native view taking input — no pointerdown reaches the page —
 *  so a split leaf listens for this to make itself the active leaf, and the
 *  cell's card to select the cell (StackItem), as a click would. */
export const GUEST_FOCUS_EVENT = 'collimate:guest-focus';

/** A native renderer's guest pool key: cell + renderer + file + per-mount
 *  sequence. The file is part of the key because one cell can host the
 *  same renderer for two files at once (two tabs, two split leaves). */
export function guestPoolId(cellId: string | undefined, rendererId: string, fileName: string, seq: number): string {
    return `guest::${cellId ?? ''}::${rendererId}::${fileName}::${seq}`;
}

/** True when `poolId` (see guestPoolId) hosts a guest for `fileName` of
 *  `cellId` — the reclaim match when that file is gone. */
export function isGuestOfFile(poolId: string, cellId: string, fileName: string): boolean {
    return poolId.startsWith(`guest::${cellId}::`) && poolId.includes(`::${fileName}::`);
}

export interface GuestMountSpec {
  /** Pool key. Two mounts naming the same one share a guest, so it has to
   *  be stable across re-mounts and distinct between concurrent hosts. */
  poolId: string;
  /** The URL the guest is built on. Creation-time configuration, like the
   *  partition: pointing an existing guest somewhere else is
   *  `handle.loadURL`, not a re-mount. */
  src: string;
  /** Bare partition name. {@link partitionFor} sanitises and prefixes it —
   *  the one place that happens. */
  partition: string;
  /** The device the guest presents as (the shell composes its user agent
   *  and emulation); absent = desktop. A parked guest reattached under
   *  another device is switched to it. */
  device?: GuestDevice;
  /** Exempt from LRU, still loaded while parked, and never muted while
   *  hidden (a daemon may depend on the page behaving like a visible one).
   *  Holds an OS process until the pool reclaims it explicitly: its file
   *  leaves the cell (CellEditor → destroyGuestsWhere(isGuestOfFile)), a
   *  mount arrives under the same poolId with a different guestSessionKey, or
   *  the page unloads. LRU never takes it, and `destroy()` only parks; see
   *  the destroy comment below. */
  keepAlive?: boolean;
  /** Whether the guest takes pointer input. Defaults to true. */
  interactive?: boolean;
  /** The logical session this guest belongs to. A mount whose key differs
   *  from the parked entry's destroys that guest and creates a fresh one;
   *  a matching key (undefined included) reattaches. */
  guestSessionKey?: string;
  /** Cell hosting this mount, for the pool's per-cell z bump, occlusion
   *  scoping and conceal wiring. */
  cellId?: string;
}

/**
 * The mount handle the host keeps, beyond what renderers see: conceal is
 * host plumbing (NativeRenderer wiring RendererConcealContext through),
 * not a renderer verb.
 */
export interface HostGuestHandle extends GuestHandle {
  /** Hide the guest with its host cell's conceal animations. */
  setConcealed(concealed: boolean): void;
}

/** The device each guest this page created presents as, by guest id: a
 *  mount that reattaches a parked guest switches it when the two differ.
 *  An entry lives while its pool still holds that guest under its pool id;
 *  each creation first drops the entries of guests the pools have since
 *  destroyed, evicted or replaced. */
const guestDevices = new Map<string, { device: GuestDevice; pool: GuestPoolAPI; poolId: string }>();

function forgetGoneGuests(): void {
  for (const [guestId, entry] of guestDevices) {
    if (entry.pool.getGuestId(entry.poolId) !== guestId) guestDevices.delete(guestId);
  }
}

// Handlers are stored erased: `on` is typed per kind at the call site, and
// a Set can only hold one signature.
type ErasedGuestHandler = (data: GuestEventData[GuestEventKind]) => void;

export function mountGuestInto(
  pool: GuestPoolAPI,
  hostEl: HTMLElement,
  spec: GuestMountSpec,
): HostGuestHandle {
  const { poolId, src, partition, cellId } = spec;
  // Mutable: setDevice changes it, and a guest the pool recreates after an
  // eviction is built on the device in force.
  let device: GuestDevice = spec.device ?? 'desktop';
  const keepAlive = spec.keepAlive === true;
  const interactive = spec.interactive !== false;

  const shell = desktopBridge();
  // A build without native guest support. Refusing loudly beats handing
  // back a handle whose every call quietly does nothing.
  if (!shell || !hasCap('nativeGuests')) {
    return refusedHandle('mountGuest: this build has no native guest support');
  }

  // The id is minted HERE, before the shell has done anything — that is
  // what lets events route from the first push and layout batches name
  // the guest before creation resolves (the shell drops those quietly).
  // The create closure runs once per pool-entry creation, including after
  // an eviction, so every run mints a fresh id and a fresh view.
  let inflight: Promise<string> | null = null;
  const create = (): string => {
    const guestId = `${poolId}::${Date.now().toString(36)}-${Math.floor(Math.random() * 0xffff).toString(16)}`;
    forgetGoneGuests();
    guestDevices.set(guestId, { device, pool, poolId });
    inflight = shell.guest
      .createGuest({
        guestId,
        partition: partitionFor(partition),
        url: src,
        device,
        // Policy decided here, mechanism in the shell: an ordinary page
        // must not keep playing audio under its screenshot; a keepAlive
        // guest is exactly the one whose realism is the point.
        muteWhenHidden: !keepAlive,
      })
      .then(() => {
        // Any layout batch sent before the view existed was dropped —
        // re-send so a created-at-rest guest can be shown at real bounds.
        pool.refreshGuestLayout(poolId);
        return guestId;
      });
    // Surfaced through handle.guestId; an unconsumed rejection would be
    // an unhandled-rejection console error for a failure the caller may
    // legitimately not care about.
    inflight.catch(() => undefined);
    return guestId;
  };

  const acquiredId = pool.acquireGuest(
    poolId,
    {
      kind: 'guest',
      create,
      keepAlive,
      interactive,
      ...(spec.guestSessionKey !== undefined ? { guestSessionKey: spec.guestSessionKey } : {}),
    },
    hostEl,
    cellId,
  );
  // Reused parked entry: its guest already exists, `create` never ran.
  const guestIdPromise: Promise<string> =
    inflight ?? (acquiredId !== null ? Promise.resolve(acquiredId) : Promise.reject(new Error(`mountGuest ${poolId}: the pool has no overlay to host a guest`)));
  guestIdPromise.catch(() => undefined);
  // A reattached guest built for another device is switched in place.
  if (inflight === null && acquiredId !== null && (guestDevices.get(acquiredId)?.device ?? 'desktop') !== device) {
    guestDevices.set(acquiredId, { device, pool, poolId });
    shell.guest.setGuestDevice(acquiredId, device).catch(err => console.error('guest device switch failed:', err));
  }

  // Canvas pan/zoom has to be able to claim the surface, or a drag that
  // starts over the placeholder is swallowed by it. SandboxIframe does
  // this by folding MovingContext's `isMoving` into its setPointerEvents
  // call; MovingProvider mirrors the same state onto window events
  // precisely so non-React code — a renderer module's mount, which has no
  // context to read — can follow it too. (The native view freezes to its
  // placeholder the moment a pan starts, so during motion the placeholder
  // is the only input surface in play.)
  let wantsPointerEvents = interactive;
  let panning = false;
  const applyPointerEvents = (): void => pool.setPointerEvents(poolId, wantsPointerEvents && !panning);
  const onPanStart = (): void => { panning = true; applyPointerEvents(); };
  const onPanEnd = (): void => { panning = false; applyPointerEvents(); };
  window.addEventListener('canvas-pan-start', onPanStart);
  window.addEventListener('canvas-pan-end', onPanEnd);
  applyPointerEvents();

  let destroyed = false;

  const handlers = new Map<GuestEventKind, Set<ErasedGuestHandler>>();
  const dispatch = (event: GuestEvent): void => {
    const subscribers = handlers.get(event.kind);
    if (!subscribers) return;
    for (const handler of Array.from(subscribers)) {
      try { handler(event.data); } catch (err) { console.error('guest event handler error:', err); }
    }
  };

  // One stream carries every guest in the window; this mount's id is known
  // from the start, so routing is a straight filter — no buffering, no
  // replay. `ready` doubles as the pool's readiness signal (there is no
  // element to fire dom-ready any more) and as the reveal trigger; a
  // crash hides the view AND the placeholder so the renderer's own crash
  // UI — plain cell DOM the overlay normally paints over — is visible,
  // and the next successful load (a reload through the handle) restores.
  const unsubscribeShell = shell.onGuestEvent((event) => {
    if (destroyed || event.guestId !== pool.getGuestId(poolId)) return;
    if (event.kind === 'crashed') {
      pool.setGuestCrashed(poolId, true);
    } else if (event.kind === 'ready') {
      pool.setGuestCrashed(poolId, false);
      pool.setGuestReady(poolId);
      pool.setVisible(poolId, true, hostEl);
    } else if (event.kind === 'focus' && event.data.focused) {
      hostEl.dispatchEvent(new CustomEvent(GUEST_FOCUS_EVENT, { bubbles: true, composed: true }));
    } else if (event.kind === 'wheel' && isFocusWheel(event.data)) {
      // The native view took the wheel and scrolled; the page never saw it.
      // Re-emitted on the host, the card and leaf around it focus on it as
      // on any wheel over an unfocused cell. Only a focusing wheel: sideways
      // travel inside a guest is the guest's own (re-emitted, the canvas
      // swipe would step the selection under a page scrolling sideways),
      // and ctrl-wheel is its zoom.
      const { deltaX, deltaY } = event.data;
      hostEl.dispatchEvent(new WheelEvent('wheel', { bubbles: true, composed: true, deltaX, deltaY, deltaMode: 0 }));
    }
    dispatch(event);
  });

  const currentGuestId = (): string | null => pool.getGuestId(poolId);
  const withGuest = async <T>(op: (guestId: string) => Promise<T>): Promise<T> => {
    const id = currentGuestId();
    if (id === null) throw new Error(`guest ${poolId}: no guest is mounted`);
    return op(id);
  };

  return {
    get guestId(): Promise<string> {
      return guestIdPromise;
    },
    loadURL: (url: string) => withGuest((id) => shell.guest.navigate(id, url)),
    goBack: () => withGuest((id) => shell.guest.goBack(id)),
    goForward: () => withGuest((id) => shell.guest.goForward(id)),
    reload: () => withGuest((id) => shell.guest.reload(id)),
    setDevice: (next: GuestDevice) => withGuest(async (id) => {
      device = next;
      guestDevices.set(id, { device: next, pool, poolId });
      await shell.guest.setGuestDevice(id, next);
    }),
    evaluate: (js: string) => withGuest((id) => shell.guest.evaluate(id, js)),
    capturePng: () => withGuest((id) => shell.guest.capturePng(id)),
    currentUrl: () => withGuest((id) => shell.guest.currentUrl(id)),
    on<K extends GuestEventKind>(kind: K, handler: (data: GuestEventData[K]) => void): () => void {
      let subscribers = handlers.get(kind);
      if (!subscribers) {
        subscribers = new Set();
        handlers.set(kind, subscribers);
      }
      const erased = handler as ErasedGuestHandler;
      subscribers.add(erased);
      return () => {
        subscribers.delete(erased);
        if (subscribers.size === 0) handlers.delete(kind);
      };
    },
    setInteractive: (on: boolean) => {
      wantsPointerEvents = on;
      applyPointerEvents();
      // The presentation half: a non-interactive guest can't be LIVE (a
      // visible native view swallows clicks), so it drops to the
      // refreshed-screenshot mode; back to interactive re-arms LIVE.
      pool.setGuestInteractive(poolId, on);
    },
    setConcealed: (concealed: boolean) => {
      pool.setGuestConcealed(poolId, concealed);
    },
    destroy: () => {
      if (destroyed) return;
      destroyed = true;
      unsubscribeShell();
      window.removeEventListener('canvas-pan-start', onPanStart);
      window.removeEventListener('canvas-pan-end', onPanEnd);
      handlers.clear();
      // Destroy releases the MOUNT, never the guest: a mount dies on every
      // renderer remount (cell scrolled back, moved between stacks, zoom
      // transitions), and the whole value of the pool is that the page —
      // its current URL, history, logins — survives those. The entry
      // parks; a re-mount with the same poolId reattaches to the same
      // guest. Reclamation is the pool's: LRU for ordinary guests, and
      // for keepAlive ones an explicit reclaim by the host (CellEditor's
      // destroyGuestsWhere once the owning file is gone), a new guestSessionKey
      // on acquire, or page unload. A mount's own destroy never reclaims
      // one; that is what keepAlive means. (Also load-bearing for the drop
      // race: on a cell drop, the floating drag preview's mount and the
      // landed cell's mount briefly share this poolId, and the loser's
      // destroy must not tear down the entry the winner just acquired.)
      pool.release(poolId, hostEl);
    },
  };
}

/** A handle that answers honestly where guests don't exist. */
function refusedHandle(reason: string): HostGuestHandle {
  const refuse = (): Promise<never> => Promise.reject(new Error(reason));
  const rejected: Promise<string> = Promise.reject(new Error(reason));
  // Nobody may ever read .guestId; an untouched rejection must not crash
  // the console for a browser tab that simply has no shell.
  rejected.catch(() => undefined);
  return {
    get guestId(): Promise<string> {
      return rejected;
    },
    loadURL: refuse,
    goBack: refuse,
    goForward: refuse,
    reload: refuse,
    // No guest, no device to present: nothing to switch.
    setDevice: async () => {},
    evaluate: refuse,
    capturePng: refuse,
    currentUrl: refuse,
    on: () => () => {},
    setInteractive: () => {},
    setConcealed: () => {},
    destroy: () => {},
  };
}
