/** Geometry and CSSOM cache echoes for pooled surface placement. */
import type { GuestLayoutRect } from './desktop';
import { snapToDevice } from './deviceOrigin';
import type { GuestKind } from '@/contexts/GuestPoolContext';

export interface PoolEntry {
  id: string;
  kind: GuestKind;
  /** iframe only: the document currently loaded. A guest's URL is not
   *  the pool's business — navigation goes through the shell's guest ops
   *  (see GuestHandle.loadURL). */
  src: string;
  disposeIframe?: (target:Window)=>void;
  container: HTMLElement | null;
  /** The overlay element: the iframe itself, or a guest's placeholder. */
  element: HTMLElement;
  /** guest only: the screenshot inside the placeholder. */
  img: HTMLImageElement | null;
  /** guest only: the renderer-minted id every shell op takes. Fixed for
   *  the entry's lifetime — an evicted entry's replacement mints anew. */
  guestId: string | null;
  /** Never evicted by LRU; survives parking with its page still loaded.
   *  Only an explicit destroy (``destroyGuestsWhere`` / ``destroyGuest``),
   *  such as a host whose .webview file was removed, or an acquire naming
   *  a different session reclaims one. */
  keepAlive: boolean;
  /** guest only: the session the page belongs to, as its creator named it.
   *  Compared on every acquire; see the session check in acquireGuest. */
  guestSessionKey: string | undefined;
  /** guest only: LIVE-eligibility. A non-interactive guest stays FROZEN
   *  with an adaptive 1–5 second screenshot refresh — a visible native view cannot pass
   *  clicks through itself. */
  interactive: boolean;
  /** guest only: the consumer asked the placeholder to show (page painted). */
  wantsVisible: boolean;
  /** What the consumer asked for with setPointerEvents. The element's real
   *  pointer-events also depends on occlusion and conceal (see
   *  applyGuestPresentation): an occluded placeholder is click-through so
   *  the DOM beneath it — a drop zone, an overlay's own edge — gets the
   *  pointer. */
  wantsPointerEvents: boolean;
  /** guest only: host cell is mid-conceal (burn/dissolve/drag-hide) or
   *  offscreen — placeholder hidden AND view hidden, no capture. */
  concealed: boolean;
  /** guest only: the renderer process died. View and placeholder both
   *  hide — their pixels are a dead page's, and the mount's crash UI
   *  lives in cell DOM the overlay would otherwise cover. Cleared by the
   *  next successful load. A separate bit from `concealed` because the
   *  writers are different (the shell's event stream vs the host cell's
   *  animations) and neither may clobber the other. */
  crashed: boolean;
  /** guest only: the native view is currently the presentation. */
  live: boolean;
  /** guest only: bumps on every freeze/thaw so a stale async capture
   *  can't hide a view that was re-thawed meanwhile. */
  freezeToken: number;
  /** guest only: object URL currently shown in `img`. */
  captureUrl: string | null;
  /** guest only: the paint still waiting on `img` to load. The img is
   *  shared, so a newer paint supersedes it: settles it and revokes its
   *  URL, rather than leaving its promise (and a freeze's hide) hanging. */
  pendingPaint: { url: string; settle: (ok: boolean) => void } | null;
  /** guest only: the non-interactive refresh interval. */
  refreshTimer: number | null;
  refreshDelay: number;
  nextRefreshAt: number;
  captureFingerprint: string | null;
  pendingFilesMessage: Record<string, unknown> | null;
  pendingChangedPaths: Set<string> | null;
  resizeObserver: ResizeObserver | null;
  lastUsed: number;
  /** Cell currently displaying this entry — used by
   *  ``bringCellsToFront`` to bump z-index per-cell while a
   *  stack is animating, and by the occlusion registry to scope freezes.
   *  Updated on every ``acquire`` because the same poolId can roam across
   *  cells (LRU pool keyed by renderer+file). */
  cellId: string | null;
  /** Flipped true after the surface is ready (an iframe's `load`, a
   *  guest's main-process `ready` event via setGuestReady). Once loaded,
   *  the entry stays "ready" for its lifetime — src swaps reset this back
   *  to false so consumers fade in again on file change. */
  ready: boolean;
  readyResolvers: Array<() => void>;
  /** Last x/y/w/h written to element.style, in overlay-local coords. Used
   *  by the sync loop to skip the write phase when a frame's computed
   *  rect matches the previous frame — and, since the scheduler landed,
   *  as the pool's quiescence signal: a pass that beats no entry's cache
   *  is a frame in which nothing moved.
   *
   *  The cache is only as good as its claim about the element, so EVERY
   *  positioning write goes through `applyRect`, which updates it — the
   *  sync pass and the out-of-band `positionOver` alike. It used to be
   *  pass-only: `positionOver` (acquire, the ResizeObserver, overlay
   *  re-registration) wrote the element behind the cache's back.
   *  One such write of a rect the container only held for an instant,
   *  followed by the container coming back to the rect the cache already
   *  held, and every later pass — the verify tick included — compared
   *  "container vs cache", found them equal and called the scene clean
   *  while the renderer sat offset from its cell for good. See
   *  `appliedTransform` for the backstop against writes the pool never
   *  made at all. */
  lastX: number;
  lastY: number;
  lastW: number;
  lastH: number;
  /** Last border-radius written — part of the same skip-cache. Card-corner
   *  rounding keeps a flush guest corner from covering the card's rounded
   *  border arc (see cardCornerRadius). */
  lastR: string;

  /** What `element.style` read back as right after the pool's own last
   *  positioning write (read back, not the string written: CSSOM
   *  re-serializes numbers, so only its own echo compares exactly). The
   *  pass treats an element that no longer holds these as dirty whatever
   *  the rect cache says — parkOffscreen, or any writer outside the pool,
   *  can then displace an iframe for at most one pass (≤ one verify tick
   *  while the loop is parked) instead of indefinitely. Inline-style
   *  reads, so checking them costs no layout. */
  appliedTransform: string;
  appliedWidth: string;
  appliedHeight: string;
  /** guest only: last layout emitted to the shell, in window CSS px.
   *  Doubles as the motion detector — a beat here means the guest is
   *  moving on screen (camera included), which is what freezes it. */
  emittedX: number;
  emittedY: number;
  emittedW: number;
  emittedH: number;
  emittedRadius: number;
}

export interface CornerRadius {
  /** Per-corner CSS shorthand for the DOM element. */
  css: string;
  /** The largest corner, for the native view's uniform setBorderRadius. */
  max: number;
}

const SQUARE_CORNERS: CornerRadius = { css: '0px', max: 0 };

// An overlay element covers its container's exact (square) rect, but the
// container sits just inside a rounded, bordered card — and the overlay
// paints ABOVE the card (z 110 vs 100). Wherever a container corner
// coincides with a card corner, the element's square corner would cover the
// card's border arc, eating the border at the corners. Round exactly those
// corners to the card's inner radius (outer radius minus the border) so the
// border reads the same thickness all the way around; corners that sit
// mid-card (under a tab bar, above a split divider) stay square. The `max`
// leg feeds the native view, which can only round uniformly.
export function cardCornerRadius(container: HTMLElement, cRect: DOMRect, scale: number): CornerRadius {
  const card = container.closest('[data-stack-card]');
  if (!card) return SQUARE_CORNERS;
  const kRect = card.getBoundingClientRect();
  const cardStyle = getComputedStyle(card);
  const outer = parseFloat(cardStyle.borderTopLeftRadius) || 0;
  if (outer <= 0) return SQUARE_CORNERS;
  const inner = Math.max(0, outer - (parseFloat(cardStyle.borderTopWidth) || 0));
  // Coincidence tolerance: the container sits at least the card's border
  // inside its rect. Rects are viewport-space, so scale the tolerance;
  // the radius itself is layout-space (transforms don't touch it).
  const tol = 3 * scale;
  const near = (a: number, b: number) => Math.abs(a - b) <= tol;
  const tl = near(cRect.left, kRect.left) && near(cRect.top, kRect.top) ? inner : 0;
  const tr = near(cRect.right, kRect.right) && near(cRect.top, kRect.top) ? inner : 0;
  const br = near(cRect.right, kRect.right) && near(cRect.bottom, kRect.bottom) ? inner : 0;
  const bl = near(cRect.left, kRect.left) && near(cRect.bottom, kRect.bottom) ? inner : 0;
  return { css: `${tl}px ${tr}px ${br}px ${bl}px`, max: Math.max(tl, tr, br, bl) };
}

/** An entry's overlay-local placement, as the pool writes it. */
export interface OverlayRect {
  x: number;
  y: number;
  w: number;
  h: number;
  radius: CornerRadius;
}

/** The overlay's layout width. It has no height, so it covers nothing; its
 *  width is a ruler for the camera's scale (`overlayScale`). */
export const OVERLAY_RULER_PX = 1000;

/** The camera's CSS scale at the overlay: its on-screen width (`oRect`)
 *  over its layout width. Overlay-local coordinates are screen deltas
 *  divided by this, so any error in it grows with the distance from the
 *  overlay's origin. Measured on the overlay's own exact width, never an
 *  integer-rounded one such as the pan container's `offsetWidth`, which
 *  read a 1522.4px canvas as 1522px and put a frame 565px from the origin
 *  0.15px off its container. */
export function overlayScale(oRect: DOMRect): number {
  return oRect.width > 0 ? oRect.width / OVERLAY_RULER_PX : 1;
}

export { snapToDevice };

/** Marks the camera's layout origin: the element the camera's transforms
 *  (and the overlay inside them) hang from, itself untransformed. */
export const CAMERA_ORIGIN_ATTR = 'data-camera-origin';

/** The device-pixel grid a pooled frame is placed on. Chromium paints a
 *  frame at its layout origin rounded to the device pixel, plus its
 *  transforms' translation; the translation is never rounded. So a frame
 *  sits on whole device pixels exactly when the translation from the
 *  camera's layout origin to it is whole device pixels — snapping its
 *  viewport position alone left it half a pixel off under a 70px top bar
 *  at 1.25×. */
export interface PixelGrid {
  dpr: number;
  /** The camera's layout origin, viewport CSS px. */
  originX: number;
  originY: number;
}

/** The grid for frames in `overlay`, as laid out right now. */
export function pixelGridOf(overlay: HTMLElement): PixelGrid {
  const origin = overlay.closest(`[${CAMERA_ORIGIN_ATTR}]`)?.getBoundingClientRect();
  return { dpr: window.devicePixelRatio, originX: origin?.left ?? 0, originY: origin?.top ?? 0 };
}

/** Where `container` puts its element, in `overlay`-local coordinates:
 *  the container's whole rect.
 *
 *  A frame's origin lands on `grid`: a frame drawn off whole device pixels
 *  is resampled, and its text reads soft. A guest placeholder keeps its
 *  exact rect: the native view's own snapping contract (`guestLayoutOf`)
 *  decides where its pixels land. */
export function overlayRectOf(
  cRect: DOMRect, container: HTMLElement, oRect: DOMRect, scale: number, kind: GuestKind, grid: PixelGrid,
): OverlayRect {
  const { dpr, originX, originY } = grid;
  const iframe = kind === 'iframe';
  const left = iframe ? originX + snapToDevice(cRect.left - originX, dpr) : cRect.left;
  const top = iframe ? originY + snapToDevice(cRect.top - originY, dpr) : cRect.top;
  // The far edges snap on their own: carried by the near edge's rounding,
  // the right or bottom edge reached half a device pixel into the card's
  // border, which Chromium paints on whole device pixels — that border
  // pixel showed thin, or not at all, beside the frame.
  const right = iframe ? originX + snapToDevice(cRect.right - originX, dpr) : cRect.right;
  const bottom = iframe ? originY + snapToDevice(cRect.bottom - originY, dpr) : cRect.bottom;
  return {
    x: (left - oRect.left) / scale,
    y: (top - oRect.top) / scale,
    w: (right - left) / scale,
    h: (bottom - top) / scale,
    radius: cardCornerRadius(container, cRect, scale),
  };
}

/** The element still holds exactly what the pool last wrote, and that
 *  was `r`. False means a write is due — the rect moved, or something
 *  displaced the element since (see `appliedTransform`). */
export function holdsRect(entry: PoolEntry, r: OverlayRect): boolean {
  const s = entry.element.style;
  return (
    entry.lastX === r.x && entry.lastY === r.y && entry.lastW === r.w && entry.lastH === r.h &&
    entry.lastR === r.radius.css &&
    s.transform === entry.appliedTransform && s.width === entry.appliedWidth && s.height === entry.appliedHeight
  );
}

/** A placement at (`toX`, `toY`) moves a surface already placed at
 *  (`fromX`, `fromY`) — the motion that earns the scene's motion layers
 *  (lib/motionLayers). A first placement (the cache is NaN) is not one,
 *  and neither is a write that keeps the position: a resize or a new corner
 *  radius changes the surface where it stands. Promoting for those flipped the
 *  canvas's text from its resting raster to the layer's and back: at 1.25×
 *  every glyph dropped a device pixel and lost its subpixel antialiasing
 *  for the ~200 ms to the settle edge, a bounce on each selection. */
export function movesSurface(fromX: number, fromY: number, toX: number, toY: number): boolean {
  if (Number.isNaN(fromX) || Number.isNaN(fromY)) return false;
  return fromX !== toX || fromY !== toY;
}

/** The one positioning write. Keeps the rect cache and the read-back
 *  echo in step with the element, so the sync pass can trust them. */
export function applyRect(entry: PoolEntry, r: OverlayRect): void {
  const s = entry.element.style;
  s.width = `${r.w}px`;
  s.height = `${r.h}px`;
  s.transform = `translate(${r.x}px, ${r.y}px)`;
  s.borderRadius = r.radius.css;
  entry.lastX = r.x;
  entry.lastY = r.y;
  entry.lastW = r.w;
  entry.lastH = r.h;
  entry.lastR = r.radius.css;
  entry.appliedTransform = s.transform;
  entry.appliedWidth = s.width;
  entry.appliedHeight = s.height;
}

/** Out-of-band positioning — acquire, the ResizeObserver, overlay
 *  re-registration: land the rect on THIS frame rather than wait
 *  for the loop. Through `applyRect` like the pass, never around it (see
 *  `PoolEntry.lastX` for what writing around the cache cost). */
export function positionOver(entry: PoolEntry, container: HTMLElement, overlay: HTMLElement): void {
  const oRect = overlay.getBoundingClientRect();
  const r = overlayRectOf(
    container.getBoundingClientRect(), container, oRect, overlayScale(oRect), entry.kind, pixelGridOf(overlay),
  );
  // Visibility is NOT set here on purpose — positioning must not reveal an
  // element that hasn't loaded yet. The world-space overlay sits above the
  // SandboxIframe container (z-index 110 vs 100), so any visible element
  // occludes the container's loading spinner. Revealing a still-loading
  // iframe here painted the browser's empty-document white over the
  // spinner (the "white flash on mount"). Visibility flips in setVisible
  // (driven by the consumer's RENDERER_READY / safety-net path) and in
  // `acquire` (when un-parking an entry that is already ready).
  if (!holdsRect(entry, r)) applyRect(entry, r);
}

/** The renderer's snapping half of the layout contract: floor to the
 *  window-zoom grid so the shell's `Math.round(v * z)` is an identity and
 *  the native view lands on the pixels the placeholder occupies. */
export function snapToZoom(value: number, zoom: number): number {
  return Math.floor(value * zoom) / zoom;
}

/** A guest's window rect as the shell receives it: snapped per the
 *  contract above, the radius in window px. It covers the container, as
 *  its placeholder does. */
export function guestLayoutOf(guestId: string, cRect: DOMRect, radius: CornerRadius, scale: number, zoom: number): GuestLayoutRect {
  return {
    guestId,
    x: snapToZoom(cRect.left, zoom),
    y: snapToZoom(cRect.top, zoom),
    w: snapToZoom(cRect.width, zoom),
    h: snapToZoom(cRect.height, zoom),
    radius: Math.round(radius.max * scale),
  };
}

/** The last layout emitted for this entry is exactly `g`. */
export function holdsEmitted(e: PoolEntry, g: GuestLayoutRect): boolean {
  return (
    e.emittedX === g.x && e.emittedY === g.y && e.emittedW === g.w && e.emittedH === g.h &&
    e.emittedRadius === g.radius
  );
}

export function recordEmitted(e: PoolEntry, g: GuestLayoutRect): void {
  e.emittedX = g.x;
  e.emittedY = g.y;
  e.emittedW = g.w;
  e.emittedH = g.h;
  e.emittedRadius = g.radius;
}

