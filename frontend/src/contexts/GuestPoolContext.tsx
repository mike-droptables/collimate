/**
 * GuestPoolContext — LRU warm pool for the canvas's embedded surfaces.
 *
 * Two kinds live here. `iframe` entries are the sandbox renderers the pool
 * was built for: real elements, positioned inside the world-space overlay
 * and moved by the sync loop. `guest` entries are native Chromium views
 * (main-process WebContentsViews, desktop shell only) — for those the pool
 * holds a *placeholder* div in the overlay and the pixels live in a native
 * view the OS composites on top of the whole window. Both kinds are
 * acquired, parked, revealed and evicted by the same code; the kind
 * decides what exists behind the element.
 *
 * The iframe overlay math is unchanged: elements are absolutely positioned
 * within a world-space overlay that shares the canvas transform, so
 * pan/zoom ride the compositor and the sync loop only tracks motion
 * *within* world space. Iframe elements are still never re-parented (a
 * move costs a reload); guest placeholders are plain divs and move freely.
 *
 * Guests add a second geometry problem the overlay can't solve: the
 * native view knows nothing about CSS transforms, so it needs absolute
 * window coordinates, pushed over IPC (`layoutGuests`), and it is always
 * painted above every piece of DOM. The pool's answer is a presentation
 * state machine per guest:
 *
 *   LIVE    — the native view is visible at the placeholder's window rect.
 *             Entered only when ALL hold: the scene is at rest (the sync
 *             scheduler's quiescence edge), canvas scale == 1, nothing in
 *             the occlusion registry covers the guest's cell, the host
 *             cell isn't concealed, and the guest is interactive.
 *   FROZEN  — the view is hidden (its page keeps running); the
 *             placeholder shows the last capture and pans/zooms/animates
 *             in the compositor exactly like an iframe.
 *
 * Freezing is capture → paint placeholder → next frame hide (so the
 * freshest pixels are on screen before the swap); thawing is layout →
 * show (the placeholder stays painted underneath — the view covers it).
 * Motion is detected in the sync pass itself: a guest whose *window* rect
 * moved freezes on the spot, which is also what keeps the loop awake
 * through camera motion that leaves overlay-local coordinates untouched.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { cancelFrame, frame } from 'motion/react';
import { desktopBridge, type GuestLayoutRect } from '@/lib/desktop';
import {
  overlayScale, overlayRectOf, pixelGridOf, cardCornerRadius, holdsRect, movesSurface, applyRect, positionOver, guestLayoutOf, holdsEmitted,
  recordEmitted, type PoolEntry, type OverlayRect,
} from '@/lib/guestPoolGeometry';
import { isCellOccluded, subscribeGuestOcclusion } from '@/lib/guestOcclusion';
import { createMotionLayers } from '@/lib/motionLayers';
import {
  createGuestSyncScheduler,
  guestSyncAlwaysRun,
  type GuestSyncScheduler,
  type GuestSyncState,
  type GuestSyncStats,
} from '@/lib/guestSyncScheduler';

// Warm-pool sizing for parked (released) iframes. The floor keeps small
// channels snappy on scroll-back; scaling with the number of concurrently
// ACTIVE iframes keeps busy channels — which will plausibly re-visit as many
// renderers as they show at once — from thrashing full iframe reloads.
const SLEEPING_IFRAME_FLOOR = 20;

// The same, for guests. A parked guest's placeholder is just a div, but the
// native view behind it is an OS process with its own renderer, GPU
// allocations and network stack — the DOM stopped being the constraint when
// guests moved to WebContentsViews, but the process count didn't. A guest is
// also far cheaper to lose than a sandbox renderer — re-acquiring one
// re-navigates a page rather than re-running a renderer's whole setup — so
// the pool keeps only a handful warm and lets the rest go.
const SLEEPING_GUEST_FLOOR = 4;

/** Refresh cadence for a visible non-interactive guest's screenshot. */
const NONINTERACTIVE_REFRESH_MS = 1000;
const MAX_NONINTERACTIVE_REFRESH_MS = 5000;

// Debug handle for the sync loop — dev builds only. `state()` should read
// 'idle' on a still canvas and `stats().missedWakes` should stay 0; a
// non-zero one names a pool mutation whose wake source we never wired.
declare global {
  interface Window {
    __collimateGuestSync?: {
      state: () => GuestSyncState;
      stats: () => GuestSyncStats;
      invalidate: () => void;
    };
  }
}

/** What kind of surface an entry hosts. */
export type GuestKind = 'iframe' | 'guest';

/** What to create, when a pool id is acquired for the first time. */
export type GuestSpec =
  | { kind: 'iframe'; src: string; dispose?: (target:Window)=>void }
  | {
      kind: 'guest';
      /** Mint a fresh shell-side guest and return the renderer-minted id.
       *  Called once per entry creation — including after an eviction, so
       *  it must mint a fresh id and a fresh `createGuest` call every
       *  time rather than closing over one. */
      create: () => string;
      /** Exempt from LRU eviction, and stays loaded while parked. */
      keepAlive?: boolean;
      /** Whether the guest may take input (and so go LIVE); default true. */
      interactive?: boolean;
      /** The logical session this guest belongs to. Acquiring a parked
       *  entry whose key differs drops it — guest and all — and creates
       *  anew; an equal key (undefined included) reattaches. */
      guestSessionKey?: string;
    };

const SLEEPING_FLOOR: Record<GuestKind, number> = {
  iframe: SLEEPING_IFRAME_FLOOR,
  guest: SLEEPING_GUEST_FLOOR,
};

export interface GuestPoolAPI {
  /** Map an iframe (by id) to a visible container; creates the iframe if new. */
  acquire: (id: string, src: string, container: HTMLElement, cellId?: string, dispose?: (target:Window)=>void) => void;
  /** ``acquire`` for entries whose surface the pool can't build itself.
   *  Same ownership, parking and positioning semantics; the spec decides
   *  what gets created on first acquire and whether LRU may reclaim it.
   *  For guest specs, resolves to the entry's guest id (existing or
   *  freshly minted); null when the entry couldn't be created. */
  acquireGuest: (id: string, spec: GuestSpec, container: HTMLElement, cellId?: string) => string | null;
  /** Unmap from container — hides the element off-screen in the overlay.
   *  Pass the same container ref the consumer used in `acquire` so a
   *  stale release (e.g. an exiting SandboxIframe whose entry was
   *  already re-acquired by a sibling for the same poolId) becomes a
   *  no-op instead of parking a live iframe. */
  release: (id: string, expectedContainer?: HTMLElement) => void;
  /** Remove an ephemeral surface, with the same stale-owner guard as release. */
  drop: (id: string, expectedContainer?: HTMLElement) => void;
  /** Drop an entry and its surface for good.
   *
   *  The counterpart to `release` for `keepAlive` entries: LRU never
   *  reclaims those, so without an explicit destroy a parked guest holds
   *  its OS process for the life of the page. Nothing calls it from
   *  `GuestHandle.destroy`; that only releases the mount (see guestMount).
   *  Hosts reclaim keepAlive guests with `destroyGuestsWhere` when the file
   *  that owns them goes away, or implicitly with an acquire that names a
   *  different guestSessionKey. */
  destroyGuest: (id: string) => void;
  /** destroyGuest every entry whose pool id matches — how a host reclaims
   *  the keepAlive guests of a file that no longer exists (a run's
   *  `.webview` tab file removed at its end). */
  destroyGuestsWhere: (match: (id: string) => boolean) => void;
  /** The container an entry's element (a pooled iframe, a guest
   *  placeholder) is currently positioned over, or null. The element
   *  lives in the world-space overlay, outside its host's DOM; this maps
   *  it back (e.g. to find the split leaf an iframe that took focus
   *  belongs to). */
  containerOf: (element: Element) => HTMLElement | null;
  /** Enable or disable pointer events on the element (e.g. during canvas pan). */
  setPointerEvents: (id: string, enabled: boolean) => void;
  /** Get the iframe's contentWindow for postMessage calls. Always null for
   *  a guest: it is a separate process behind the shell's bridge, not a
   *  same-origin window this page can post into. */
  getContentWindow: (id: string) => WindowProxy | null;
  /** The entry's element, for consumers that need its own DOM events. */
  getElement: (id: string) => HTMLElement | null;
  /** A guest entry's renderer-minted id, or null when there is no such
   *  entry (or it isn't a guest). The string every shell guest op takes. */
  getGuestId: (id: string) => string | null;
  /** Mark a guest entry ready (its first main-frame load finished). The
   *  mount calls this off the shell's `ready` event — the pool has no
   *  element event to hear it from any more. */
  setGuestReady: (id: string) => void;
  /** LIVE-eligibility for a guest: false parks it in FROZEN with a ~1 Hz
   *  screenshot refresh and a click-through placeholder. */
  setGuestInteractive: (id: string, interactive: boolean) => void;
  /** Conceal a guest during its host cell's hide animations (burn,
   *  dissolve, drag-hide, offscreen) — placeholder hidden, view hidden,
   *  nothing captured. The placeholder lives in the world-space overlay,
   *  so the cell's own opacity/scale can't reach it; this is the guest
   *  half of RendererConcealContext. */
  setGuestConcealed: (id: string, concealed: boolean) => void;
  /** Mark a guest's renderer process dead (or recovered). While crashed,
   *  the view and the placeholder both hide so the mount's crash UI —
   *  plain cell DOM the overlay normally paints over — is actually
   *  visible. The mount drives this off the shell's `crashed` event and
   *  clears it on the next `ready`. */
  setGuestCrashed: (id: string, crashed: boolean) => void;
  /** Blank a guest's emitted-layout cache so the next pass re-sends its
   *  rect. Called by the mount once `createGuest` resolves — a layout
   *  batch sent before the view existed was silently dropped. */
  refreshGuestLayout: (id: string) => void;
  /** Broadcast a message only to iframes hosting the given cell (plus
   *  entries acquired without a cellId). Use for cell-scoped messages
   *  like FILES_CHANGED so a commit in one cell doesn't make every
   *  mounted renderer refetch its files. */
  broadcastToCell: (cellId: string, message: object) => void;
  /** Register the world-space overlay anchor (called by StackScene). */
  registerOverlay: (el: HTMLElement | null) => void;
  /** "Something in the scene may have moved" — wakes the sync loop.
   *  Idempotent and cheap (see lib/guestSyncScheduler): the pool would
   *  rather run a handful of passes that find nothing than miss one.
   *  Every pool mutation calls it already; consumers only need it for
   *  motion the pool can't see, which today is the React commit that
   *  starts a layout spring (wired in StackScene). */
  invalidate: () => void;
  /** The camera is moving, or is about to: promote every motion layer
   *  (lib/motionLayers) and wake the sync loop. The layers stay promoted
   *  until the loop's settle edge. StackScene calls it on every camera
   *  value change and on the start of every camera spring, so the
   *  promotion is in place before the first moved frame paints. */
  markMoving: () => void;
  /** Track an element whose transform moves with the scene (the camera's
   *  wrappers, a renderer's host box): it is promoted to its own
   *  compositor layer while the scene moves and demoted at rest. Returns
   *  the untrack. The pool's own surfaces are tracked already. */
  registerMotionLayer: (element: HTMLElement) => () => void;
  /** Resolves when the entry associated with `id` has fired its first
   *  readiness event (an iframe's `load`, a guest's `ready` push). If it
   *  already fired, resolves synchronously on the next microtask. Used by
   *  SandboxIframe to fade in once the renderer is painted, avoiding the
   *  unstyled-flash. */
  waitUntilReady: (id: string) => Promise<void>;
  /** Toggle element visibility. The element stays hidden from creation
   *  through sandbox.html load and the renderer's own setup (loading
   *  CSS, mounting React, first paint). The SandboxIframe consumer
   *  flips this true only when RENDERER_READY is received (or the
   *  3s safety-net fallback fires); a guest mount flips it on the
   *  shell's `ready` event. Pass the caller's container so a stale
   *  consumer (same poolId, different cell — the fan-out toggle
   *  double-mount window) can't toggle the current owner's element. */
  setVisible: (id: string, visible: boolean, expectedContainer?: HTMLElement) => void;
  /** Lift the entries hosting these cells to the top of the world-
   *  space overlay's stacking context (z 1, vs default 0 for the
   *  rest), and raise their native views in the window's view z-order.
   *  Used while a stack is being dragged or animating after a drop so
   *  its renderer paints above sibling entries during the pass instead
   *  of being decided by DOM-insertion order. Pass an empty set to clear
   *  all bumps. */
  bringCellsToFront: (cellIds: Set<string>) => void;
  /** Subscribe to postMessages sent by THIS entry's iframe (matched on
   *  e.source, same-origin only). The pool owns a single window-level
   *  `message` listener and dispatches — one listener total instead of
   *  one per mounted SandboxIframe, each of which previously received
   *  and source-filtered every message from every iframe. Returns an
   *  unsubscribe. */
  onMessage: (id: string, handler: (e: MessageEvent) => void) => () => void;
}

const GuestPoolContext = createContext<GuestPoolAPI>({
  acquire: () => {},
  acquireGuest: () => null,
  release: () => {},
  drop: () => {},
  destroyGuest: () => {},
  destroyGuestsWhere: () => {},
  containerOf: () => null,
  setPointerEvents: () => {},
  getContentWindow: () => null,
  getElement: () => null,
  getGuestId: () => null,
  setGuestReady: () => {},
  setGuestInteractive: () => {},
  setGuestConcealed: () => {},
  setGuestCrashed: () => {},
  refreshGuestLayout: () => {},
  broadcastToCell: () => {},
  registerOverlay: () => {},
  invalidate: () => {},
  markMoving: () => {},
  registerMotionLayer: () => () => {},
  waitUntilReady: () => Promise.resolve(),
  setVisible: () => {},
  bringCellsToFront: () => {},
  onMessage: () => () => {},
});

export function useGuestPool(): GuestPoolAPI {
  return useContext(GuestPoolContext);
}

// Reveal / hide helpers. Hiding is instant (visibility cuts the paint on the
// same frame); revealing flips visibility and lets the element's CSS opacity
// transition fade it in, so a pool re-acquire during a cell's entry animation
// doesn't pop a fully-painted renderer over a still-fading-in card.
//
// `visibility`, never `display`: for an iframe, display:none would throw
// away layout the next reveal has to redo (and historically it detached
// `<webview>` guests outright — the placeholder is indifferent, but there
// is no reason for the two kinds to diverge).
function revealElement(element: HTMLElement): void {
  element.style.visibility = 'visible';
  element.style.opacity = '1';
}

function hideElement(element: HTMLElement): void {
  element.style.visibility = 'hidden';
  element.style.opacity = '0';
}

function parkOffscreen(element: HTMLElement): void {
  element.style.transform = 'translate(-9999px, -9999px)';
  element.style.pointerEvents = 'none';
  hideElement(element);
}

/** Opacity of a guest placeholder while an occluder covers its cell: the
 *  page stays recognisable, and whatever paints beneath the overlay layer
 *  (a tab drag's drop highlight) shows through. */
const OCCLUDED_PLACEHOLDER_OPACITY = '0.35';

// Shared positioning styles. Guest placeholders add overflow:hidden so the
// screenshot clips to the rounded corners the card rounding writes.
const OVERLAY_ELEMENT_CSS =
  'position:absolute;top:0;left:0;border:none;pointer-events:auto;visibility:hidden;opacity:0;transition:opacity 0.18s ease-out;';

/** The window a message from this entry would arrive with — iframes only. */
function contentWindowOf(entry: PoolEntry): WindowProxy | null {
  if (entry.kind !== 'iframe') return null;
  return (entry.element as HTMLIFrameElement).contentWindow;
}

/** Flip an entry ready and resolve everyone waiting on it. */
function markReady(entry: PoolEntry): void {
  entry.ready = true;
  const waiters = entry.readyResolvers;
  entry.readyResolvers = [];
  for (const r of waiters) r();
}

/** Settle an entry's in-flight placeholder paint as not-loaded; its own
 *  continuation revokes the URL. */
function settlePendingPaint(entry: PoolEntry): void {
  const paint = entry.pendingPaint;
  if (!paint) return;
  entry.pendingPaint = null;
  paint.settle(false);
}

function scheduleNextFrame(cb: () => void): void {
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => cb());
  else setTimeout(cb, 16);
}

export function GuestPoolProvider({ children }: { children: React.ReactNode }) {
  const overlayRef = useRef<HTMLElement | null>(null);
  const entries = useRef<Map<string, PoolEntry>>(new Map());
  const messageHandlers = useRef<Map<string, Set<(e: MessageEvent) => void>>>(new Map());
  /** Canvas scale as of the last sync pass; LIVE requires exactly 1. */
  const lastScaleRef = useRef(1);
  const zoomRef = useRef(desktopBridge()?.getZoomFactor() ?? 1);

  // Single window-level dispatcher for iframe → host postMessages. Matches
  // e.source against the registered entries' contentWindows and invokes only
  // that entry's handlers.
  useEffect(() => {
    const onWindowMessage = (e: MessageEvent) => {
      if (e.origin !== window.location.origin) return;
      if (!e.source) return;
      for (const [id, handlers] of messageHandlers.current) {
        const entry = entries.current.get(id);
        if (!entry || contentWindowOf(entry) !== e.source) continue;
        for (const handler of Array.from(handlers)) {
          try { handler(e); } catch (err) { console.error('iframe message handler error:', err); }
        }
        break; // a message has exactly one source iframe
      }
    };
    window.addEventListener('message', onWindowMessage);
    return () => window.removeEventListener('message', onWindowMessage);
  }, []);

  const onMessage = useCallback((id: string, handler: (e: MessageEvent) => void) => {
    const map = messageHandlers.current;
    let handlers = map.get(id);
    if (!handlers) {
      handlers = new Set();
      map.set(id, handlers);
    }
    handlers.add(handler);
    return () => {
      handlers.delete(handler);
      if (handlers.size === 0) map.delete(id);
    };
  }, []);

  // The scheduler is created in an effect below, but child effects run
  // FIRST — a SandboxIframe or a guest mount can acquire before it exists.
  // The ref indirection makes that a dropped wake instead of a crash, and
  // the effect fires an invalidate on creation to cover the gap.
  const schedulerRef = useRef<GuestSyncScheduler | null>(null);
  const invalidate = useCallback(() => {
    schedulerRef.current?.invalidate();
  }, []);

  // Compositor promotion for everything the scene moves: on while it
  // moves, off at the sync loop's settle edge (see lib/motionLayers).
  const [motionLayers] = useState(createMotionLayers);
  const markMoving = useCallback(() => {
    motionLayers.promote();
    invalidate();
  }, [motionLayers, invalidate]);
  const registerMotionLayer = useCallback((element: HTMLElement) => {
    motionLayers.track(element);
    return () => motionLayers.untrack(element);
  }, [motionLayers]);

  // -------------------------------------------------------------------------
  // Guest presentation: LIVE <-> FROZEN
  // -------------------------------------------------------------------------

  /** Paint transient JPEG85 bytes into the placeholder. Always settles: a
   *  newer paint on the same entry supersedes this one (see
   *  `PoolEntry.pendingPaint`). */
  const paintPlaceholder = useCallback(async (entry: PoolEntry, jpeg: ArrayBuffer): Promise<void> => {
    const img = entry.img;
    if (!img || typeof URL.createObjectURL !== 'function') return;
    settlePendingPaint(entry);
    const url = URL.createObjectURL(new Blob([jpeg], { type: 'image/jpeg' }));
    const loaded = await new Promise<boolean>((resolve) => {
      let settled = false;
      const paint = {
        url,
        settle: (ok: boolean) => {
          if (settled) return;
          settled = true;
          if (entry.pendingPaint === paint) {
            entry.pendingPaint = null;
            img.onload = null;
            img.onerror = null;
          }
          resolve(ok);
        },
      };
      entry.pendingPaint = paint;
      img.onload = () => paint.settle(true);
      img.onerror = () => paint.settle(false);
      img.src = url;
    });
    if (!loaded) {
      URL.revokeObjectURL(url);
      return;
    }
    if (entry.captureUrl) URL.revokeObjectURL(entry.captureUrl);
    entry.captureUrl = url;
    img.style.visibility = 'visible';
  }, []);

  /** Capture the guest now and paint it into the placeholder. */
  const refreshCapture = useCallback((entry: PoolEntry): void => {
    const shell = desktopBridge();
    const guestId = entry.guestId;
    if (!shell || !guestId || !entry.ready) return;
    // Nobody sees a hidden window's placeholders; the visibilitychange
    // listener in the scheduler effect repaints them on the way back.
    if (document.visibilityState === 'hidden') return;
    void shell.guest
      .capturePlaceholder(guestId)
      .then((jpeg) => {
        if (entry.guestId !== guestId) return;
        let hash = 2166136261;
        for (const byte of new Uint8Array(jpeg)) hash = Math.imul(hash ^ byte, 16777619);
        const fingerprint = `${jpeg.byteLength}:${hash >>> 0}`;
        entry.refreshDelay = fingerprint === entry.captureFingerprint
          ? Math.min(MAX_NONINTERACTIVE_REFRESH_MS, entry.refreshDelay * 2)
          : NONINTERACTIVE_REFRESH_MS;
        entry.captureFingerprint = fingerprint;
        entry.nextRefreshAt = Date.now() + entry.refreshDelay;
        return paintPlaceholder(entry, jpeg);
      })
      .catch(() => { /* a dead or mid-navigation guest; keep the old frame */ });
  }, [paintPlaceholder]);

  /**
   * FROZEN. With `capture`, VS Code's deferred hide: capture → paint the
   * placeholder → wait a frame (freshest pixels are on screen) → hide the
   * view. Without it (conceal, park), the view hides on the spot.
   */
  const freezeGuest = useCallback((entry: PoolEntry, options?: { capture?: boolean }): void => {
    if (entry.kind !== 'guest' || !entry.live) return;
    entry.live = false;
    const token = ++entry.freezeToken;
    const shell = desktopBridge();
    const guestId = entry.guestId;
    if (!shell || !guestId) return;
    const hide = () => {
      if (entry.freezeToken !== token || entry.live || entry.guestId !== guestId) return;
      void shell.guest.setGuestVisible(guestId, false);
    };
    if (options?.capture === false) {
      hide();
      return;
    }
    shell.guest
      .capturePlaceholder(guestId)
      .then(async (jpeg) => {
        if (entry.freezeToken !== token || entry.live || entry.guestId !== guestId) return;
        await paintPlaceholder(entry, jpeg);
        scheduleNextFrame(hide);
      })
      .catch(hide);
  }, [paintPlaceholder]);

  /** Every LIVE condition, in one place. */
  const guestMayGoLive = useCallback((entry: PoolEntry): boolean => {
    return (
      entry.kind === 'guest' &&
      entry.guestId !== null &&
      entry.ready &&
      entry.container !== null &&
      entry.wantsVisible &&
      !entry.concealed &&
      !entry.crashed &&
      entry.interactive &&
      Math.abs(lastScaleRef.current - 1) < 0.001 &&
      !isCellOccluded(entry.cellId) &&
      (schedulerRef.current?.atRest() ?? false)
    );
  }, []);

  /** One guest's rect, emitted immediately (the thaw path's "layout
   *  before show"). Reads layout — never called from the sync pass, which
   *  batches its own reads. */
  const emitEntryLayout = useCallback((entry: PoolEntry): void => {
    const shell = desktopBridge();
    if (!shell || entry.kind !== 'guest' || !entry.guestId || !entry.container) return;
    const cRect = entry.container.getBoundingClientRect();
    const scale = lastScaleRef.current;
    // The radius the sync pass places the placeholder with, so the two paths
    // emit identical layouts and the pass never reads a thaw as motion.
    const radius = cardCornerRadius(entry.container, cRect, scale);
    const g = guestLayoutOf(entry.guestId, cRect, radius, scale, zoomRef.current);
    recordEmitted(entry, g);
    shell.guest.layoutGuests({ guests: [g], zoomFactor: zoomRef.current });
  }, []);

  /** LIVE. Bounds first (the shell defers showing until laid out), then
   *  show; the placeholder stays painted underneath — the view covers it. */
  const thawGuest = useCallback((entry: PoolEntry): void => {
    if (entry.live || !guestMayGoLive(entry)) return;
    const shell = desktopBridge();
    if (!shell || !entry.guestId) return;
    entry.live = true;
    entry.freezeToken++;
    emitEntryLayout(entry);
    void shell.guest.setGuestVisible(entry.guestId, true);
  }, [emitEntryLayout, guestMayGoLive]);

  /** Reconcile one guest's presentation with its eligibility, and keep the
   *  non-interactive refresh cadence in step. */
  const evaluateGuest = useCallback((entry: PoolEntry): void => {
    if (entry.kind !== 'guest') return;
    if (!entry.live && guestMayGoLive(entry)) {
      thawGuest(entry);
    } else if (entry.live && !guestMayGoLive(entry)) {
      freezeGuest(entry, { capture: !entry.concealed });
    }

    const wantsRefresh =
      !entry.interactive &&
      !entry.live &&
      entry.ready &&
      entry.guestId !== null &&
      entry.container !== null &&
      entry.wantsVisible &&
      !entry.concealed &&
      !entry.crashed;
    if (wantsRefresh && entry.refreshTimer === null) {
      refreshCapture(entry);
      entry.nextRefreshAt = Date.now() + entry.refreshDelay;
      entry.refreshTimer = window.setInterval(() => {
        if (Date.now() >= entry.nextRefreshAt) {
          entry.nextRefreshAt = Date.now() + entry.refreshDelay;
          refreshCapture(entry);
        }
      }, NONINTERACTIVE_REFRESH_MS);
    } else if (!wantsRefresh && entry.refreshTimer !== null) {
      window.clearInterval(entry.refreshTimer);
      entry.refreshTimer = null;
    }
  }, [freezeGuest, guestMayGoLive, refreshCapture, thawGuest]);

  const evaluateAllGuests = useCallback((): void => {
    entries.current.forEach((entry) => evaluateGuest(entry));
  }, [evaluateGuest]);

  /** An occluded guest's placeholder is inert scenery: something is
   *  painting above it (a menu, a dialog, a cell's drop highlight during a
   *  tab drag). It stays painted — the page remains recognisable — but
   *  dimmed, so the in-cell DOM beneath (which the overlay would otherwise
   *  cover) reads through it, and click-through, so that DOM receives the
   *  pointer and drag events. Concealed placeholders are hidden anyway;
   *  they are made click-through too so a hide in flight never eats a
   *  drop. */
  const applyGuestPresentation = useCallback((entry: PoolEntry): void => {
    if (entry.kind !== 'guest') return;
    const occluded = isCellOccluded(entry.cellId);
    const passive = occluded || entry.concealed;
    entry.element.style.pointerEvents = entry.wantsPointerEvents && !passive ? 'auto' : 'none';
    if (entry.element.style.visibility === 'visible') {
      entry.element.style.opacity = occluded ? OCCLUDED_PLACEHOLDER_OPACITY : '1';
    }
  }, []);

  /** The placeholder's effective visibility: what the consumer asked for,
   *  minus conceal. (Parking overrides both via parkOffscreen.) */
  const applyGuestElementVisibility = useCallback((entry: PoolEntry): void => {
    if (entry.kind !== 'guest') return;
    if (entry.wantsVisible && !entry.concealed && !entry.crashed && entry.container) revealElement(entry.element);
    else hideElement(entry.element);
    applyGuestPresentation(entry);
  }, []);

  // Drop an entry and the surface it owns. For an iframe, removing the
  // element is what reclaims the document; for a guest, the shell-side
  // destroy is what reclaims the OS process.
  const dropEntry = useCallback((entry: PoolEntry) => {
    entry.resizeObserver?.disconnect();
    if (entry.refreshTimer !== null) {
      window.clearInterval(entry.refreshTimer);
      entry.refreshTimer = null;
    }
    if (entry.captureUrl) {
      URL.revokeObjectURL(entry.captureUrl);
      entry.captureUrl = null;
    }
    settlePendingPaint(entry);
    if (entry.kind === 'guest' && entry.guestId) {
      const shell = desktopBridge();
      void shell?.guest.destroyGuest(entry.guestId).catch(() => undefined);
    }
    const target=contentWindowOf(entry);
    entries.current.delete(entry.id);
    if(target)entry.disposeIframe?.(target);
    motionLayers.untrack(entry.element);
    entry.element.remove();
  }, [motionLayers]);

  // Called by StackScene to register the overlay anchor inside the pan container
  const registerOverlay = useCallback((el: HTMLElement | null) => {
    if (el && el !== overlayRef.current) {
      const oldOverlay = overlayRef.current;
      if (oldOverlay) {
        // Reparenting reloads an iframe while retaining its WindowProxy.
        // Replace its physical document and retire the old source first.
        for(const entry of Array.from(entries.current.values())){
          if(entry.kind!=='iframe'){el.appendChild(entry.element);continue;}
          const old=entry.element,target=contentWindowOf(entry);
          entries.current.delete(entry.id);
          if(target)entry.disposeIframe?.(target);
          motionLayers.untrack(old);
          old.remove();
          const iframe=document.createElement('iframe');iframe.src=entry.src;
          iframe.setAttribute('sandbox','allow-scripts allow-same-origin allow-forms');
          iframe.style.cssText=old.style.cssText;motionLayers.track(iframe);
          entry.element=iframe;entry.ready=false;hideElement(iframe);
          iframe.addEventListener('load',()=>{if(entry.element===iframe)markReady(entry);});
          el.appendChild(iframe);entries.current.set(entry.id,entry);
        }
      }
      overlayRef.current = el;
      // Reposition all active entries
      entries.current.forEach((entry) => {
        if (entry.container) positionOver(entry, entry.container, el);
      });
      invalidate();
    }
  }, [invalidate, motionLayers]);

  // One sync pass — runs under frame.postRender so layout is read *after*
  // Framer Motion has applied its CSS transforms for the current frame.
  // We batch reads (getBoundingClientRect) and writes (style) to prevent layout thrashing.
  // Per-entry lastX/Y/W/H cache lets us skip style writes (and the string
  // construction they require) on frames where a container's rect hasn't
  // changed — the common case while idle (and, for overlay-local coords,
  // during pan/zoom, where they are invariant under the shared transform).
  //
  // The guest step rides the same read phase but compares *window* rects,
  // which camera motion does change — that beat is what freezes a LIVE
  // guest and what keeps the loop counting the camera as motion at all.
  //
  // Returns whether anything was re-written, which is the scheduler's
  // quiescence signal — see the wiring effect below. A pass that moved a
  // surface (a new position, not just a new size or radius: movesSurface)
  // also promotes the motion layers, before this frame paints.
  const runSyncPass = useCallback((): boolean => {
    const overlay = overlayRef.current;
    if (!overlay || entries.current.size === 0) return false;
    // Read phase
    const oRect = overlay.getBoundingClientRect();
    const scale = overlayScale(oRect);
    const grid = pixelGridOf(overlay);
    lastScaleRef.current = scale;

    // Read ALL container rects before writing ANY style. Interleaving
    // (read, write, read, write, …) invalidates layout on every write,
    // forcing a reflow for the next entry's read — with many visible
    // iframes that's N reflows per frame during stack move/resize
    // animations, the main dropped-frames source in busy scenes.
    const dirty: Array<{ e: PoolEntry; r: OverlayRect }> = [];
    const batch: GuestLayoutRect[] = [];
    let guestDirty = false;
    let translated = false;
    entries.current.forEach((e) => {
      if (!e.container) return;
      const cRect = e.container.getBoundingClientRect();
      const r = overlayRectOf(cRect, e.container, oRect, scale, e.kind, grid);
      // Dirty against what the element actually holds, not just against
      // the last rect this pass computed — see PoolEntry.lastX.
      if (!holdsRect(e, r)) {
        dirty.push({ e, r });
        if (movesSurface(e.lastX, e.lastY, r.x, r.y)) translated = true;
      }

      // Guest step: absolute window coordinates for the native view, on
      // the renderer's half of the snapping contract.
      if (e.kind === 'guest' && e.guestId !== null && !e.concealed) {
        const g = guestLayoutOf(e.guestId, cRect, r.radius, scale, zoomRef.current);
        if (!holdsEmitted(e, g)) {
          const firstLayout = Number.isNaN(e.emittedX);
          const resized = e.emittedW !== g.w || e.emittedH !== g.h;
          const wasLive = e.live;
          if (movesSurface(e.emittedX, e.emittedY, g.x, g.y)) translated = true;
          // Remember motion even when no shell layout is needed, so a still
          // frozen guest can let the scheduler settle. Thaw sends fresh bounds.
          recordEmitted(e, g);
          if (firstLayout || wasLive || resized) batch.push(g);
          guestDirty = true;
          // The guest is moving on screen under a live native view: freeze
          // it now (capture-first, so the stand-in is fresh). The very
          // first layout is position, not motion — nothing to freeze.
          if (e.live && !firstLayout) freezeGuest(e, { capture: true });
        }
      }
    });
    for (const { e, r } of dirty) applyRect(e, r);
    if (batch.length > 0) {
      desktopBridge()?.guest.layoutGuests({ guests: batch, zoomFactor: zoomRef.current });
    }
    if (translated) motionLayers.promote();
    return dirty.length > 0 || guestDirty;
  }, [freezeGuest, motionLayers]);

  // The pass used to reschedule itself unconditionally. Motion's batcher
  // keeps its whole frameloop alive for a standing callback, so that one
  // line meant a permanent rAF running all eight steps — on an empty pool,
  // on a motionless canvas, forever. The scheduler runs the same pass on
  // the same frame hook, but parks once the pass reports N clean frames
  // and wakes on invalidation; an idle canvas now schedules nothing at all.
  //
  // Wake sources are every pool mutation (below), window resize, the
  // scene-invalidated broadcast, camera motion and every React commit
  // (the last two wired in StackScene).
  //
  // The settle edge doubles as the scene's "at rest" signal: parked means
  // still, and still is when frozen guests may go LIVE and when the motion
  // layers come off the compositor, to be rasterized afresh where they lie.
  useEffect(() => {
    const sched = createGuestSyncScheduler({
      pass: runSyncPass,
      scheduleFrame: (cb) => frame.postRender(cb),
      cancelFrame: (cb) => cancelFrame(cb),
      // A plain interval, never a rAF: waking the batcher on a timer
      // would put back the always-on frameloop this exists to remove.
      setInterval: (cb, ms) => window.setInterval(cb, ms),
      clearInterval: (handle) => window.clearInterval(handle),
      alwaysRun: guestSyncAlwaysRun(),
      onSettle: () => {
        motionLayers.demote();
        evaluateAllGuests();
      },
      onMissedWake: import.meta.env.DEV
        ? () => console.warn(
            '[guest-pool] the sync loop was parked while a guest had moved — ' +
            'the verify tick fixed it, but some mutation is missing an invalidate() call',
          )
        : undefined,
    });
    schedulerRef.current = sched;
    // Child effects run before the provider's, so a SandboxIframe or a
    // guest mount may already have acquired into a null schedulerRef.
    // Start awake and let the settle decide.
    sched.invalidate();

    const unsubscribeZoom = desktopBridge()?.onZoomChanged((factor) => {
      if (factor === zoomRef.current) return;
      zoomRef.current = factor;
      // A factor change needs a fresh native layout even if the CSS rectangle
      // happens to be unchanged. Existing scheduling keeps layout before show.
      entries.current.forEach((entry) => { entry.emittedX = NaN; });
      sched.invalidate();
    });
    const wake = () => sched.invalidate();
    // A window resize moves every container without any React state
    // changing, and the scene-invalidated broadcast is the app's existing
    // "the layout you measured is stale" signal (StackApp fires it around
    // fan-out toggles).
    window.addEventListener('resize', wake);
    window.addEventListener('collimate:scene-invalidated', wake);

    // A pan gesture is the one motion source that announces itself before
    // the first moved pixel — freeze ahead of it and the captures happen
    // while the pixels are still fresh, not mid-smear.
    const onPanStart = () => {
      entries.current.forEach((entry) => {
        if (entry.kind === 'guest' && entry.live) freezeGuest(entry, { capture: true });
      });
      motionLayers.promote();
      sched.invalidate();
    };
    window.addEventListener('canvas-pan-start', onPanStart);

    // refreshCapture skips while the window is hidden; a refreshing guest
    // gets a fresh frame the moment it is visible again rather than up to
    // one refresh interval later.
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') return;
      entries.current.forEach((e) => {
        if (e.refreshTimer !== null) refreshCapture(e);
      });
    };
    document.addEventListener('visibilitychange', onVisibility);

    // Overlays coming and going re-decide LIVE eligibility on the spot —
    // an occluded live guest freezes, an uncovered frozen one may thaw
    // (the scene is often already at rest when a menu closes).
    const unsubscribeOcclusion = subscribeGuestOcclusion(() => {
      entries.current.forEach((entry) => applyGuestPresentation(entry));
      evaluateAllGuests();
    });

    if (import.meta.env.DEV) {
      window.__collimateGuestSync = {
        state: sched.state,
        stats: sched.stats,
        invalidate: sched.invalidate,
      };
    }

    return () => {
      unsubscribeZoom?.();
      window.removeEventListener('resize', wake);
      window.removeEventListener('collimate:scene-invalidated', wake);
      window.removeEventListener('canvas-pan-start', onPanStart);
      document.removeEventListener('visibilitychange', onVisibility);
      unsubscribeOcclusion();
      sched.stop();
      schedulerRef.current = null;
      if (import.meta.env.DEV) delete window.__collimateGuestSync;
      // Snapshot first: dropEntry deletes from the map.
      Array.from(entries.current.values()).forEach(dropEntry);
      overlayRef.current = null;
    };
  }, [applyGuestPresentation, dropEntry, evaluateAllGuests, freezeGuest, motionLayers, refreshCapture, runSyncPass]);

  const acquireGuest = useCallback((id: string, spec: GuestSpec, container: HTMLElement, cellId?: string): string | null => {
    const overlay = overlayRef.current;
    if (!overlay) return null;

    const map = entries.current;
    let entry = map.get(id);

    // A pool id survives its session. Re-mounting one is normally exactly
    // what the caller wants — a cell closed and reopened mid-session gets
    // its live page back — but a NEW session under the same id (a web cell
    // re-run in place: same cell, same renderer, same mount order, so the
    // same id) must not be handed its predecessor's page, silently ignoring
    // the src it asked for. Different key: the old guest goes, and goes
    // properly — LRU never reclaims a keepAlive entry, so nothing else
    // would ever have.
    if (entry && entry.kind === 'guest' && spec.kind === 'guest' && entry.guestSessionKey !== spec.guestSessionKey) {
      dropEntry(entry);
      entry = undefined;
    }

    // Navigation is a new physical document; queued messages from its old WindowProxy must not register again.
    if(entry&&entry.kind==='iframe'&&spec.kind==='iframe'&&entry.src!==spec.src){dropEntry(entry);entry=undefined;}

    // Set on a reattach whose spec flips LIVE-eligibility; new entries
    // take theirs from the spec at creation.
    let interactiveChanged = false;

    if (!entry) {
      // LRU eviction: drop the oldest sleeping entry when over the cap.
      // Single pass — count sleepers and track the oldest as we go.
      //
      // Counted per KIND, and with keepAlive entries invisible to the scan
      // entirely (neither counted nor evictable). Kinds are separate
      // because their caps mean different things — a handful of guests
      // must not be able to evict the iframe pool, or twenty iframes to
      // evict a guest — and keepAlive entries are out because the whole
      // point of one is that nothing reclaims it behind its owner's back.
      let sleepingCount = 0;
      let activeCount = 0;
      let oldest: PoolEntry | null = null;
      map.forEach((e) => {
        if (e.kind !== spec.kind || e.keepAlive) return;
        if (e.container !== null) {
          activeCount++;
          return;
        }
        sleepingCount++;
        if (!oldest || e.lastUsed < oldest.lastUsed) oldest = e;
      });
      const sleepingCap = Math.max(SLEEPING_FLOOR[spec.kind], activeCount);
      if (sleepingCount >= sleepingCap && oldest) {
        const evict: PoolEntry = oldest;
        dropEntry(evict);
      }

      // Create the surface once. An iframe element never moves again; a
      // guest placeholder is free to, but the shell-side guest behind it
      // is created exactly here and lives until eviction or destroy.
      let element: HTMLElement;
      let img: HTMLImageElement | null = null;
      let guestId: string | null = null;
      if (spec.kind === 'iframe') {
        const iframe = document.createElement('iframe');
        iframe.src = spec.src;
        iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
        element = iframe;
      } else {
        // The placeholder: a clipping div with the screenshot inside.
        // Transparent until the first capture lands, so the container's
        // loading spinner shows through while the page boots.
        element = document.createElement('div');
        element.style.overflow = 'hidden';
        img = document.createElement('img');
        img.alt = '';
        img.style.cssText = 'display:block;width:100%;height:100%;visibility:hidden;';
        element.appendChild(img);
        guestId = spec.create();
      }
      element.style.cssText += OVERLAY_ELEMENT_CSS;
      element.style.display = 'block';
      motionLayers.track(element);
      overlay.appendChild(element);

      entry = {
        id,
        kind: spec.kind,
        src: spec.kind === 'iframe' ? spec.src : '',
        disposeIframe:spec.kind==='iframe'?spec.dispose:undefined,
        container,
        element,
        img,
        guestId,
        keepAlive: spec.kind === 'guest' && spec.keepAlive === true,
        guestSessionKey: spec.kind === 'guest' ? spec.guestSessionKey : undefined,
        interactive: spec.kind !== 'guest' || spec.interactive !== false,
        wantsVisible: false,
        wantsPointerEvents: true,
        concealed: false,
        crashed: false,
        live: false,
        freezeToken: 0,
        captureUrl: null,
        pendingPaint: null,
        refreshTimer: null,
        refreshDelay: NONINTERACTIVE_REFRESH_MS,
        nextRefreshAt: 0,
        captureFingerprint: null,
        pendingFilesMessage: null,
        pendingChangedPaths: new Set(),
        resizeObserver: null,
        lastUsed: Date.now(),
        ready: false,
        readyResolvers: [],
        lastX: NaN,
        lastY: NaN,
        lastW: NaN,
        lastH: NaN,
        lastR: '',
        appliedTransform: '',
        appliedWidth: '',
        appliedHeight: '',
        emittedX: NaN,
        emittedY: NaN,
        emittedW: NaN,
        emittedH: NaN,
        emittedRadius: NaN,
        cellId: cellId ?? null,
      };
      // Resolve pending waiters once the surface fires its first readiness
      // event. For an iframe that is the element's `load`; a guest has no
      // element to hear it from — the mount calls setGuestReady off the
      // shell's `ready` push instead. Visibility is NOT flipped here on
      // purpose — `load` only tells us sandbox.html is in the document;
      // the renderer inside it is still loading external CSS / mounting
      // React / running first paint. Showing the iframe at this point
      // paints an empty (or partially styled) surface over the
      // SandboxIframe spinner, which is the "white page on mount"
      // regression. The consumer calls setVisible when it receives
      // RENDERER_READY (or the 3s safety-net fires).
      if (spec.kind === 'iframe') {
        const created = entry;
        element.addEventListener('load', () => {if(created.element===element)markReady(created);});
      }
      map.set(id, entry);
    } else {
      // Track the cell currently displaying this entry — the same
      // poolId can roam across cells (LRU pool keyed by renderer+file).
      if (cellId !== undefined) entry.cellId = cellId;

      // The mount applies its spec's `interactive` to pointer events on
      // every mount; the pool's LIVE-eligibility follows it the same way,
      // as setGuestInteractive does. keepAlive is creation-time, like
      // muteWhenHidden, and is not synced here.
      if (spec.kind === 'guest' && entry.kind === 'guest') {
        const interactive = spec.interactive !== false;
        if (entry.interactive !== interactive) {
          entry.interactive = interactive;
          interactiveChanged = true;
        }
      }

      if (entry.container === container) {
        positionOver(entry, container, overlay);
        entry.lastUsed = Date.now();
        if (interactiveChanged) evaluateGuest(entry);
        invalidate();
        return entry.guestId;
      }

      // entry.container === null means the entry was released (parked
      // off-screen) and is being un-parked. The element is already a
      // child of the overlay at the right DOM position; iframes must NOT
      // be re-appended — that would reload them, wiping in-memory state
      // and showing a blank frame until the next paint. Just reposition
      // and let the consumer's setVisible re-apply visibility.
      const wasParked = entry.container === null;

      entry.resizeObserver?.disconnect();
      entry.container = container;
      entry.lastUsed = Date.now();

      if (!wasParked) {
        // Switching to a genuinely different container (e.g. the floating
        // drag preview takes over the renderer while a stack is being
        // moved). Re-append to the overlay so this element paints last
        // within the overlay's stacking context — the moving stack must
        // draw on top of any stack it passes over. Free for a guest
        // placeholder; a reload an iframe accepts for the ordering.
        overlay.appendChild(entry.element);
      } else if (entry.ready) {
        // Un-parking a ready element: parkOffscreen() set visibility:hidden
        // in release(). The consumer's visibility effect runs *before* this
        // acquire on a scroll-back tick (effect declaration order), so its
        // setVisible call saw entry.container===null and no-op'd. Restore
        // visibility here so the cell isn't left showing just its background.
        // For not-yet-ready entries, the consumer's RENDERER_READY → setVisible
        // path will handle it once paint lands.
        if (entry.kind === 'guest') applyGuestElementVisibility(entry);
        else revealElement(entry.element);
      }
    }

    if (entry.pendingFilesMessage && entry.kind === 'iframe') {
      try {
        const queued = entry.pendingChangedPaths ? [...entry.pendingChangedPaths] : undefined;
        // A path an open editor has unsaved typing in waits for that save,
        // which delivers it again (lib/rendererFlush).
        const changedFiles = queued;
        if (!changedFiles || changedFiles.length || !queued?.length) {
          contentWindowOf(entry)?.postMessage({ ...entry.pendingFilesMessage, changedFiles }, window.location.origin);
        }
        entry.pendingFilesMessage = null;
        entry.pendingChangedPaths = new Set();
      } catch { /* preserve pending changes until a later acquire */ }
    }
    positionOver(entry, container, overlay);

    // Track future resizes of this container
    const ro = new ResizeObserver(() => {
      if (entry!.container && overlayRef.current) {
        // Both, deliberately. The synchronous positionOver lands the new
        // size on THIS frame, which is what a discrete resize (a sidebar
        // opening, a width-handle release) needs — waking the loop alone
        // would cost it a frame. The invalidate is for the other case: a
        // resize that is the first frame of a continuing animation, where
        // the loop then carries the rest.
        positionOver(entry!, entry!.container, overlayRef.current);
        invalidate();
      }
    });
    ro.observe(container);
    entry.resizeObserver = ro;
    if (interactiveChanged) evaluateGuest(entry);
    invalidate();
    return entry.guestId;
  }, [applyGuestElementVisibility, dropEntry, evaluateGuest, invalidate, motionLayers]);

  const acquire = useCallback((id: string, src: string, container: HTMLElement, cellId?: string,dispose?: (target:Window)=>void) => {
    acquireGuest(id, { kind: 'iframe', src,dispose }, container, cellId);
  }, [acquireGuest]);

  const release = useCallback((id: string, expectedContainer?: HTMLElement) => {
    const entry = entries.current.get(id);
    if (!entry) return;
    // Two SandboxIframes can briefly coexist for the same poolId — e.g.
    // a fan-out toggle, where a cell appears both in its real stack
    // (exiting under AnimatePresence) and in a fan column (entering), or
    // the reverse on collapse. The exiting one's
    // cleanup release fires after the entering one's acquire has already
    // taken ownership; without this guard, that stale release parks a
    // live iframe and the new owner ends up with a hidden, mis-positioned
    // entry. Skip the release if we're not the current owner.
    if (expectedContainer && entry.container !== expectedContainer) return;
    entry.resizeObserver?.disconnect();
    entry.resizeObserver = null;
    entry.container = null;
    entry.lastUsed = Date.now();
    // Parking is position and visibility only, for both kinds. It never
    // navigates, reloads or detaches anything: a parked guest is a page
    // that is still running, which is the point of parking rather than
    // evicting it. For a guest, park = FROZEN with the placeholder hidden
    // — the view hides on the spot (there is nothing to capture for).
    if (entry.kind === 'guest') {
      freezeGuest(entry, { capture: false });
      evaluateGuest(entry); // stops the non-interactive refresh
    }
    parkOffscreen(entry.element);
    // parkOffscreen clobbers the transform the rect cache claims is on
    // the element. Leave the cache standing and the next acquire can put
    // the entry back over the same rect it had before — at which point a
    // woken loop compares the new rect against a stale-but-matching cache,
    // concludes "clean" against a lie, and leaves the element parked at
    // -9999px. Blank it so the first pass after a re-acquire always writes.
    // (The read-back echo would catch the park on its own now — see
    // PoolEntry.appliedTransform — but the pool's own writes should never
    // lean on the backstop for foreign ones.)
    entry.lastX = entry.lastY = entry.lastW = entry.lastH = NaN;
    invalidate();
  }, [evaluateGuest, freezeGuest, invalidate]);

  const drop = useCallback((id: string, expectedContainer?: HTMLElement) => {
    const entry = entries.current.get(id);
    if (!entry || (expectedContainer && entry.container !== expectedContainer)) return;
    dropEntry(entry);
    invalidate();
  }, [dropEntry, invalidate]);

  const destroyGuest = useCallback((id: string) => {
    const entry = entries.current.get(id);
    if (entry) {
      dropEntry(entry);
      invalidate();
    }
  }, [dropEntry, invalidate]);

  const destroyGuestsWhere = useCallback((match: (id: string) => boolean) => {
    let dropped = false;
    for (const entry of Array.from(entries.current.values())) {
      if (!match(entry.id)) continue;
      dropEntry(entry);
      dropped = true;
    }
    if (dropped) invalidate();
  }, [dropEntry, invalidate]);

  const containerOf = useCallback((element: Element): HTMLElement | null => {
    for (const entry of entries.current.values()) {
      if (entry.element === element) return entry.container;
    }
    return null;
  }, []);

  const setPointerEvents = useCallback((id: string, enabled: boolean) => {
    const entry = entries.current.get(id);
    if (!entry) return;
    entry.wantsPointerEvents = enabled;
    if (entry.kind === 'guest') applyGuestPresentation(entry);
    else entry.element.style.pointerEvents = enabled ? 'auto' : 'none';
  }, [applyGuestPresentation]);

  const getContentWindow = useCallback((id: string): WindowProxy | null => {
    const entry = entries.current.get(id);
    return entry ? contentWindowOf(entry) : null;
  }, []);

  const getElement = useCallback((id: string): HTMLElement | null => {
    return entries.current.get(id)?.element ?? null;
  }, []);

  const getGuestId = useCallback((id: string): string | null => {
    return entries.current.get(id)?.guestId ?? null;
  }, []);

  const setGuestReady = useCallback((id: string) => {
    const entry = entries.current.get(id);
    if (!entry || entry.kind !== 'guest' || entry.ready) return;
    markReady(entry);
    // Prime the placeholder so the first freeze-out (or a mid-motion
    // reveal) has pixels to stand on, then let the state machine decide.
    refreshCapture(entry);
    evaluateGuest(entry);
  }, [evaluateGuest, refreshCapture]);

  const setGuestInteractive = useCallback((id: string, interactive: boolean) => {
    const entry = entries.current.get(id);
    if (!entry || entry.kind !== 'guest' || entry.interactive === interactive) return;
    entry.interactive = interactive;
    evaluateGuest(entry);
  }, [evaluateGuest]);

  // The two hide bits share one body. Setting either freezes with no
  // capture, placeholder and view both gone on the spot: concealed because
  // the cell is mid-animation (a capture would be a stale frame of a
  // moving card), crashed because the view's pixels are a dead page's and
  // the placeholder's a screenshot of one, and the mount's crash UI (cell
  // DOM beneath the overlay) has to be seen.
  const setGuestHideFlag = useCallback((id: string, flag: 'concealed' | 'crashed', value: boolean) => {
    const entry = entries.current.get(id);
    if (!entry || entry.kind !== 'guest' || entry[flag] === value) return;
    entry[flag] = value;
    if (value) freezeGuest(entry, { capture: false });
    applyGuestElementVisibility(entry);
    evaluateGuest(entry);
    invalidate();
  }, [applyGuestElementVisibility, evaluateGuest, freezeGuest, invalidate]);

  const setGuestConcealed = useCallback((id: string, concealed: boolean) => {
    setGuestHideFlag(id, 'concealed', concealed);
  }, [setGuestHideFlag]);

  const setGuestCrashed = useCallback((id: string, crashed: boolean) => {
    setGuestHideFlag(id, 'crashed', crashed);
  }, [setGuestHideFlag]);

  const refreshGuestLayout = useCallback((id: string) => {
    const entry = entries.current.get(id);
    if (!entry || entry.kind !== 'guest') return;
    entry.emittedX = entry.emittedY = entry.emittedW = entry.emittedH = NaN;
    entry.emittedRadius = NaN;
    invalidate();
  }, [invalidate]);

  const waitUntilReady = useCallback((id: string): Promise<void> => {
    const entry = entries.current.get(id);
    if (!entry) return Promise.resolve();
    if (entry.ready) return Promise.resolve();
    return new Promise<void>((resolve) => {
      entry.readyResolvers.push(resolve);
    });
  }, []);

  const setVisible = useCallback((id: string, visible: boolean, expectedContainer?: HTMLElement) => {
    const entry = entries.current.get(id);
    if (!entry) return;
    // Same stale-consumer guard as release(): during a fan-out toggle
    // two SandboxIframes can briefly share a poolId, and the exiting one must
    // not toggle the element the new owner controls.
    if (expectedContainer && entry.container !== expectedContainer) return;
    if (entry.kind === 'guest') {
      entry.wantsVisible = visible;
      applyGuestElementVisibility(entry);
      evaluateGuest(entry);
      invalidate();
      return;
    }
    // Only reveal once the entry also has a container to attach to —
    // showing a parked element (translated off-screen) is harmless, but
    // `visible` visibility during parking would flicker the next time
    // it's positioned.
    if (visible && entry.container) {
      revealElement(entry.element);
    } else if (!visible) {
      hideElement(entry.element);
    }
    // A reveal is usually the last step of a mount, and the element's
    // rect may not have been written since. Cheaper to wake than to
    // reason about which of the two orderings happened.
    invalidate();
  }, [applyGuestElementVisibility, evaluateGuest, invalidate]);

  // Cell-scoped broadcast for messages about one cell's files (FILES_CHANGED).
  // Posting those to every iframe made all mounted renderers refetch their
  // files on every commit anywhere in the channel — a commit burst then
  // multiplied into a channel-wide request storm. Entries whose cellId is
  // unknown (acquired without one) still receive the message so older
  // acquire paths keep working.
  const broadcastToCell = useCallback((cellId: string, message: object) => {
    entries.current.forEach((entry) => {
      if (entry.cellId !== cellId) return;
      const data = message as Record<string, unknown>;
      if (entry.kind === 'iframe' && entry.container === null && data.type === 'FILES_CHANGED') {
        entry.pendingFilesMessage = data;
        if (!Array.isArray(data.changedFiles)) entry.pendingChangedPaths = null;
        else if (entry.pendingChangedPaths) {
          for (const path of data.changedFiles) if (typeof path === 'string') entry.pendingChangedPaths.add(path);
        }
        return;
      }
      try {
        contentWindowOf(entry)?.postMessage(message, window.location.origin);
      } catch { /* cross-origin safety */ }
    });
  }, []);

  // Pooled elements inside the world-space overlay all sit at the same
  // default z (the overlay carries z 110, children inherit auto), so DOM
  // order alone decides which paints over which. That's "recency of opened
  // wins" — a stack reordered or dragged over its neighbours can be
  // hidden behind theirs simply because its iframe was inserted first.
  // ``bringCellsToFront`` lifts the elements for the named
  // cells to z 1 and resets every other one to z 0, giving the
  // moving stack a deterministic top during drag / settle / pendingMove.
  // A LIVE guest's ordering lives in the window's native view stack
  // instead, so bumped guests are also raised there.
  const bumpedCellIdsRef = useRef<Set<string>>(new Set());
  const bringCellsToFront = useCallback((cellIds: Set<string>) => {
    const prev = bumpedCellIdsRef.current;
    // Cheap equality bail — same set as last call.
    if (prev.size === cellIds.size) {
      let same = true;
      for (const id of cellIds) {
        if (!prev.has(id)) { same = false; break; }
      }
      if (same) return;
    }
    bumpedCellIdsRef.current = new Set(cellIds);
    const shell = desktopBridge();
    entries.current.forEach((entry) => {
      const shouldBump = entry.cellId !== null && cellIds.has(entry.cellId);
      const target = shouldBump ? '1' : '';
      if (entry.element.style.zIndex !== target) {
        entry.element.style.zIndex = target;
      }
      if (shouldBump && entry.kind === 'guest' && entry.guestId && entry.live && shell) {
        void shell.guest.raiseGuest(entry.guestId).catch(() => undefined);
      }
    });
    // The bump itself doesn't move anything, but every caller is a drag
    // or a settle spring — i.e. the start of exactly the motion the loop
    // has to track.
    invalidate();
  }, [invalidate]);

  // Every member is a stable useCallback, so this object is built once and
  // never changes identity. Consumers can therefore put the whole API in an
  // effect's dependency list — which a guest mount has to, since its effect
  // creates an OS process and must not re-run on an unrelated render.
  const api = useMemo<GuestPoolAPI>(() => ({
    acquire, acquireGuest, release, drop, destroyGuest, destroyGuestsWhere, containerOf,
    setPointerEvents,
    getContentWindow, getElement, getGuestId, setGuestReady, setGuestInteractive,
    setGuestConcealed, setGuestCrashed, refreshGuestLayout, broadcastToCell,
    registerOverlay, invalidate, markMoving, registerMotionLayer, waitUntilReady, setVisible, bringCellsToFront,
    onMessage,
}), [
    acquire, acquireGuest, release, drop, destroyGuest, destroyGuestsWhere, containerOf,
    setPointerEvents,
    getContentWindow, getElement, getGuestId, setGuestReady, setGuestInteractive,
    setGuestConcealed, setGuestCrashed, refreshGuestLayout, broadcastToCell,
    registerOverlay, invalidate, markMoving, registerMotionLayer, waitUntilReady, setVisible, bringCellsToFront,
    onMessage,
]);

  return (
    <GuestPoolContext.Provider value={api}>
      {children}
    </GuestPoolContext.Provider>
  );
}

