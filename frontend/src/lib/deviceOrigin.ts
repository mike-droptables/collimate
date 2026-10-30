/** The camera's layout origin on a whole device pixel.
 *
 *  Chromium paints a layer at its layout origin rounded to the device
 *  pixel, while text painted into an ancestor's layer lands on its exact
 *  position. The board root (CAMERA_ORIGIN_ATTR) hangs under the 70px top
 *  bar, 87.5 device px at 1.25×: text rasterized while the camera's layers
 *  were promoted (a recentre, the overview) sat one device pixel lower than
 *  the same text painted at rest, and settled by that pixel at the end of
 *  every camera move. With the origin on the grid, promoted and resting
 *  text share a row at any device scale.
 *
 *  The board shifts right and down by under a device pixel, through two
 *  custom properties its style reads (`top`/`left` add them, `bottom`/
 *  `right` subtract them, so its size is unchanged and nothing the camera
 *  measures off it resizes). The guest pool's `pixelGridOf` reads the
 *  shifted origin, so frames snap against the same grid. */

/** Custom properties the board's style adds to its left and top edges. */
export const ORIGIN_DX_VAR = '--camera-origin-dx';
export const ORIGIN_DY_VAR = '--camera-origin-dy';

/** Below this fraction of a device pixel, a position counts as on the grid
 *  (float noise in `v * dpr`). */
const ON_GRID = 1e-3;

/** How far past a whole device pixel `v` (CSS px) lies, as a fraction of
 *  a device pixel in [0, 1); 0 on the grid. */
export function devicePixelRemainder(v: number, dpr: number): number {
  const d = v * dpr;
  const r = d - Math.floor(d);
  return r < ON_GRID || r > 1 - ON_GRID ? 0 : r;
}

/** The CSS px to add to `v` to reach the next whole device pixel, on
 *  layout's 1/64 px grid (what a layout length keeps of it): 0 when `v` is
 *  already whole, otherwise under one device pixel. */
export function deviceAlignOffset(v: number, dpr: number): number {
  const r = devicePixelRemainder(v, dpr);
  return r === 0 ? 0 : Math.round(((1 - r) / dpr) * 64) / 64;
}

function readVar(el: HTMLElement, name: string): number {
  return parseFloat(el.style.getPropertyValue(name)) || 0;
}

/** Keep `el`'s viewport origin on a whole device pixel: measure it now,
 *  then again whenever it resizes (the board's edges are its position: a
 *  top bar, the overview's shelf or a docked pane moves an edge and resizes
 *  it), the window resizes, or the device pixel ratio changes (a window
 *  zoom, a move to another display). Returns the teardown. */
export function alignToDevicePixels(el: HTMLElement): () => void {
  const align = () => {
    const dpr = window.devicePixelRatio || 1;
    const r = el.getBoundingClientRect();
    // The position without the current shift: the written shift is a
    // multiple of 1/64, so layout keeps it exactly.
    const dx = readVar(el, ORIGIN_DX_VAR);
    const dy = readVar(el, ORIGIN_DY_VAR);
    const nx = deviceAlignOffset(r.left - dx, dpr);
    const ny = deviceAlignOffset(r.top - dy, dpr);
    if (nx !== dx) el.style.setProperty(ORIGIN_DX_VAR, `${nx}px`);
    if (ny !== dy) el.style.setProperty(ORIGIN_DY_VAR, `${ny}px`);
  };

  let mql: MediaQueryList | null = null;
  const onRatio = () => { watchRatio(); align(); };
  // A resolution query matches one ratio only: re-armed at each change.
  const watchRatio = () => {
    mql?.removeEventListener?.('change', onRatio);
    mql = typeof window.matchMedia === 'function'
      ? window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`)
      : null;
    mql?.addEventListener?.('change', onRatio);
  };

  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(align) : null;
  ro?.observe(el);
  window.addEventListener('resize', align);
  watchRatio();
  align();
  return () => {
    ro?.disconnect();
    window.removeEventListener('resize', align);
    mql?.removeEventListener?.('change', onRatio);
    mql = null;
  };
}

/** `v` (CSS px) on the nearest whole device pixel. */
export function snapToDevice(v: number, dpr: number): number {
  return Math.round(v * dpr) / dpr;
}

const TRANSLATE_PX = /translate([XY])\((-?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)px\)/g;

/** motion's `transformTemplate` for the boxes the scene moves (the camera's
 *  pan, a stack's slot, a card): its translation on whole device pixels,
 *  each frame of a spring as at rest. A transform's translation is never
 *  rounded, so a box between pixels painted its 1px borders across two
 *  device pixels at partial strength, and a promoted layer (every spring
 *  promotes one) was resampled at each sub-pixel step: the cards' side
 *  borders went dim as a stack's width sprang and came back as it
 *  settled. Under a scale (the overview's zoom) the steps are sub-pixel and
 *  do nothing visible. */
export function deviceSnappedTransform(_values: unknown, generated: string): string {
  const dpr = window.devicePixelRatio || 1;
  return generated.replace(TRANSLATE_PX, (_, axis: string, v: string) =>
    `translate${axis}(${snapToDevice(parseFloat(v), dpr)}px)`);
}
