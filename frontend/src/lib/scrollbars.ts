// The workbench's scrollbars: one sheet, a short-lived activity mark for
// visibility, and the thumb drag, which this module owns.
//
// Every scroll container in the workbench, renderers included, shows a 16px
// gutter with an 8px rounded thumb. Scroll and pointer movement reveal it;
// after a short idle interval it fades even when the pointer stays inside the
// container. The track and corner paint nothing.
//
// The sheet is one constructed stylesheet, adopted into each tree it styles
// (`adoptScrollbars`): the app's document at startup (main.tsx), every native
// renderer's shadow root (NativeRenderer), and the sandbox document, which
// loads this module as /scrollbars.js (scripts/build-scrollbars.mjs bundles
// scrollbars.document.ts into public/) and adopts it itself.
//
// The activity mark is on the scrolling element. A scroll event neither
// bubbles nor composes, so one capturing listener on each tree's root hears
// that tree's scrolls and no other's.
//
// The drag is ours. In the workbench the native thumb drag jittered and
// snapped back to where it started on release, on Linux, where Chromium
// itself never snaps a thumb back: something in the page took the drag from
// the browser. (On Windows Chromium does snap back, once the pointer strays
// too far from the track.) A press on a thumb therefore captures the pointer
// on the scroll container and moves the scroll with it, anywhere on the
// screen, until the release, where it stays. The browser's press on the
// scrollbar is cancelled (the pointerdown and its compatibility mousedown),
// so the two never fight. A press on the track pages, as the browser's does.
// The thumb's size and place are computed as the browser computes them (a
// proportion of the track, no shorter than the thumb's CSS minimum), so the
// press lands where the thumb is drawn.

/** The thumb's shortest length, as the sheet's `min-height` / `min-width`
 *  and this module's geometry; the two must agree. */
export const MIN_THUMB_PX = 24;

export const SCROLLBAR_SIZE_PX = 16;
export const SCROLLBAR_FADE_IN_MS = 180;
export const SCROLLBAR_FADE_OUT_MS = 420;
export const SCROLL_IDLE_MS = 2500;

/** The attribute a scrolling element carries until its next idle interval. */
export const SCROLLING_ATTR = 'data-scrollbar-active';

/** The attribute a scroll container carries while its thumb is dragged. */
export const DRAGGING_ATTR = 'data-scrollbar-dragging';

export const SCROLLBAR_CSS = `
@property --collimate-scrollbar-opacity { syntax: '<percentage>'; inherits: true; initial-value: 0%; }
* { --collimate-scrollbar-opacity: 0%; }
[${SCROLLING_ATTR}] { --collimate-scrollbar-opacity: 100%; }
[${DRAGGING_ATTR}] { --collimate-scrollbar-opacity: 100%; }
::-webkit-scrollbar { width: ${SCROLLBAR_SIZE_PX}px; height: ${SCROLLBAR_SIZE_PX}px; }
::-webkit-scrollbar-track, ::-webkit-scrollbar-corner { background: transparent; }
::-webkit-scrollbar-thumb { background-color: color-mix(in srgb, var(--line-2) var(--collimate-scrollbar-opacity), transparent); border: 4px solid transparent; border-radius: 8px; background-clip: padding-box; min-height: ${MIN_THUMB_PX}px; min-width: ${MIN_THUMB_PX}px; }
::-webkit-scrollbar-thumb:hover { background-color: color-mix(in srgb, var(--ink-3) var(--collimate-scrollbar-opacity), transparent); }
::-webkit-scrollbar-thumb:active, [${DRAGGING_ATTR}]::-webkit-scrollbar-thumb { background-color: color-mix(in srgb, var(--ink-2) var(--collimate-scrollbar-opacity), transparent); }
@supports not selector(::-webkit-scrollbar) { * { scrollbar-width: auto; scrollbar-color: color-mix(in srgb, var(--line-2) var(--collimate-scrollbar-opacity), transparent) transparent; } }
`;

/** How far a press on the track scrolls, as a fraction of the viewport. */
export const PAGE_FRACTION = 0.875;

// ---------------------------------------------------------------------------
// Geometry (pure; the browser's numbers in, the gutters and the thumb out)
// ---------------------------------------------------------------------------

/** What a scroll container's box reports, as `metricsOf` reads it. */
export interface ScrollMetrics {
    left: number; top: number;               // the border box's viewport position
    offsetWidth: number; offsetHeight: number;
    clientLeft: number; clientTop: number;   // the left and top borders
    borderRight: number; borderBottom: number;
    clientWidth: number; clientHeight: number;
    scrollWidth: number; scrollHeight: number;
    scrollLeft: number; scrollTop: number;
    overflowX: string; overflowY: string;    // the computed overflow on each axis
    barWidth: number; barHeight: number;     // the sheet's scrollbar thickness, as the box computes it
}

/** Whether a box shows a scrollbar on an axis: it overflows there and its
 *  overflow is one that draws a bar. */
function barred(overflow: string, scroll: number, client: number): boolean {
    return (overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay') && scroll > client;
}

export type Axis = 'x' | 'y';

/** One gutter: where its track runs (viewport px along the axis), the thumb
 *  on it, and the scroll it stands for. */
export interface Gutter {
    axis: Axis;
    trackStart: number; trackLength: number;
    thumbStart: number; thumbLength: number;
    maxScroll: number; scroll: number;
}

function thumbOn(trackStart: number, trackLength: number, client: number, scroll: number, position: number, minThumb: number): [number, number, number] {
    const maxScroll = Math.max(0, scroll - client);
    const thumbLength = Math.min(trackLength, Math.max(minThumb, trackLength * client / scroll));
    const room = trackLength - thumbLength;
    const thumbStart = trackStart + (maxScroll > 0 && room > 0 ? room * Math.min(Math.max(position, 0), maxScroll) / maxScroll : 0);
    return [thumbStart, thumbLength, maxScroll];
}

/** The gutters a box shows, with their thumbs: the vertical one along the
 *  right edge, the horizontal one along the bottom (left-to-right writing),
 *  each only where the box overflows on that axis and leaves a gutter. */
export function guttersOf(m: ScrollMetrics, minThumb: number = MIN_THUMB_PX): { x?: Gutter & { band: [number, number] }; y?: Gutter & { band: [number, number] } } {
    const out: { x?: Gutter & { band: [number, number] }; y?: Gutter & { band: [number, number] } } = {};
    // The gutter is what the client box leaves of the border box. A bar that
    // takes no room (overlay scrollbars, or a headless browser that hides
    // them) leaves nothing and lies over the content instead, so its gutter
    // is the bar's own thickness; either way the bar lies along the edge,
    // inside the border.
    const roomRight = m.offsetWidth - m.clientLeft - m.clientWidth - m.borderRight;
    const roomBottom = m.offsetHeight - m.clientTop - m.clientHeight - m.borderBottom;
    if (barred(m.overflowY, m.scrollHeight, m.clientHeight)) {
        const gutterWidth = roomRight > 0 ? roomRight : m.barWidth;
        if (gutterWidth > 0) {
            const trackStart = m.top + m.clientTop;
            const [thumbStart, thumbLength, maxScroll] = thumbOn(trackStart, m.clientHeight, m.clientHeight, m.scrollHeight, m.scrollTop, minThumb);
            const bandEnd = m.left + m.offsetWidth - m.borderRight;
            out.y = { axis: 'y', trackStart, trackLength: m.clientHeight, thumbStart, thumbLength, maxScroll, scroll: m.scrollTop, band: [bandEnd - gutterWidth, bandEnd] };
        }
    }
    if (barred(m.overflowX, m.scrollWidth, m.clientWidth)) {
        const gutterHeight = roomBottom > 0 ? roomBottom : m.barHeight;
        if (gutterHeight > 0) {
            const trackStart = m.left + m.clientLeft;
            const [thumbStart, thumbLength, maxScroll] = thumbOn(trackStart, m.clientWidth, m.clientWidth, m.scrollWidth, m.scrollLeft, minThumb);
            const bandEnd = m.top + m.offsetHeight - m.borderBottom;
            out.x = { axis: 'x', trackStart, trackLength: m.clientWidth, thumbStart, thumbLength, maxScroll, scroll: m.scrollLeft, band: [bandEnd - gutterHeight, bandEnd] };
        }
    }
    return out;
}

/** The gutter under a viewport point, and whether the point is on its thumb. */
export function gutterAt(m: ScrollMetrics, px: number, py: number, minThumb: number = MIN_THUMB_PX): { gutter: Gutter; onThumb: boolean; along: number } | null {
    const { x, y } = guttersOf(m, minThumb);
    const within = (g: Gutter & { band: [number, number] }, across: number, along: number) =>
        across >= g.band[0] && across < g.band[1] && along >= g.trackStart && along < g.trackStart + g.trackLength;
    if (y && within(y, px, py)) return { gutter: y, onThumb: py >= y.thumbStart && py < y.thumbStart + y.thumbLength, along: py };
    if (x && within(x, py, px)) return { gutter: x, onThumb: px >= x.thumbStart && px < x.thumbStart + x.thumbLength, along: px };
    return null;
}

/** The scroll a dragged thumb stands for when the pointer is at `along`,
 *  `grab` px into the thumb: the thumb follows the pointer across the
 *  track's room, clamped at the ends. */
export function dragScroll(g: Gutter, along: number, grab: number): number {
    const room = g.trackLength - g.thumbLength;
    if (room <= 0 || g.maxScroll <= 0) return g.scroll;
    const start = along - grab - g.trackStart;
    return Math.min(g.maxScroll, Math.max(0, start / room * g.maxScroll));
}

/** The metrics of an element, as the geometry wants them. */
export function metricsOf(el: HTMLElement): ScrollMetrics {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const bar = getComputedStyle(el, '::-webkit-scrollbar');
    return {
        left: rect.left, top: rect.top,
        offsetWidth: el.offsetWidth, offsetHeight: el.offsetHeight,
        clientLeft: el.clientLeft, clientTop: el.clientTop,
        borderRight: parseFloat(style.borderRightWidth) || 0, borderBottom: parseFloat(style.borderBottomWidth) || 0,
        clientWidth: el.clientWidth, clientHeight: el.clientHeight,
        scrollWidth: el.scrollWidth, scrollHeight: el.scrollHeight,
        scrollLeft: el.scrollLeft, scrollTop: el.scrollTop,
        overflowX: style.overflowX, overflowY: style.overflowY,
        barWidth: parseFloat(bar.width) || 0, barHeight: parseFloat(bar.height) || 0,
    };
}

// ---------------------------------------------------------------------------
// Installation
// ---------------------------------------------------------------------------

/** The element a scroll event is about: the scroller itself, or the document's
 *  root for a document scroll. */
export function scrollTargetOf(event: Event, root: Document | ShadowRoot): Element | null {
    const first = event.composedPath()[0];
    if (first instanceof Element) return first;
    if (first instanceof Document) return first.documentElement;
    return root instanceof Document ? root.documentElement : null;
}

/** Reveals the nearest scrollbar after a scroll or real pointer movement.
 *  The mark expires even while the pointer remains over the scroller. The
 *  registered opacity animates on the element so the thumb in its native
 *  scrollbar paints intermediate alpha in Chromium. */
export function installScrollActivity(root: Document | ShadowRoot = document, idleMs: number = SCROLL_IDLE_MS): () => void {
    type Activity = { timer: ReturnType<typeof setTimeout> | null; animation: Animation | null };
    const active = new Map<Element, Activity>();
    const animate = (el: Element, state: Activity, visible: boolean) => {
        const before = getComputedStyle(el).getPropertyValue('--collimate-scrollbar-opacity').trim() || '0%';
        state.animation?.cancel();
        el.toggleAttribute(SCROLLING_ATTR, visible);
        const after = visible ? '100%' : '0%';
        if (before === after || typeof el.animate !== 'function') {
            state.animation = null;
            if (!visible) active.delete(el);
            return;
        }
        const animation = el.animate(
            [{ '--collimate-scrollbar-opacity': before }, { '--collimate-scrollbar-opacity': after }],
            { duration: visible ? SCROLLBAR_FADE_IN_MS : SCROLLBAR_FADE_OUT_MS, easing: 'ease-out' },
        );
        state.animation = animation;
        animation.onfinish = () => {
            if (state.animation !== animation) return;
            animation.cancel();
            state.animation = null;
            if (!el.hasAttribute(SCROLLING_ATTR)) active.delete(el);
        };
    };
    const reveal = (el: Element) => {
        let state = active.get(el);
        if (!state) {
            state = { timer: null, animation: null };
            active.set(el, state);
        }
        if (!el.hasAttribute(SCROLLING_ATTR)) animate(el, state, true);
        if (state.timer !== null) clearTimeout(state.timer);
        state.timer = setTimeout(() => {
            state!.timer = null;
            if (!el.hasAttribute(DRAGGING_ATTR)) animate(el, state!, false);
        }, idleMs);
    };
    const onScroll = (event: Event) => {
        const el = scrollTargetOf(event, root);
        if (el) reveal(el);
    };
    const pointerScroller = (event: PointerEvent): Element | null => {
        const first = event.composedPath()[0];
        if (!(first instanceof Element) || first.getRootNode() !== root) return null;
        for (let el: Element | null = first; el; el = el.parentElement) {
            if (!(el instanceof HTMLElement)) continue;
            const style = getComputedStyle(el);
            if (barred(style.overflowY, el.scrollHeight, el.clientHeight) ||
                barred(style.overflowX, el.scrollWidth, el.clientWidth)) return el;
            if (root instanceof ShadowRoot && el === root.host) break;
        }
        if (root instanceof Document) {
            const page = root.scrollingElement;
            if (page && (page.scrollHeight > page.clientHeight || page.scrollWidth > page.clientWidth)) return page;
        }
        return null;
    };
    const onPointerOver = (event: PointerEvent) => {
        if (event.pointerType === 'touch') return;
        const el = pointerScroller(event);
        if (el) reveal(el);
    };
    const onPointerEnd = (event: PointerEvent) => {
        const el = pointerScroller(event);
        if (el?.hasAttribute(DRAGGING_ATTR)) reveal(el);
    };
    let lastPoint: { id: number; x: number; y: number } | null = null;
    const onPointerMove = (event: PointerEvent) => {
        if (event.pointerType === 'touch') return;
        if (lastPoint?.id === event.pointerId && lastPoint.x === event.clientX && lastPoint.y === event.clientY) return;
        lastPoint = { id: event.pointerId, x: event.clientX, y: event.clientY };
        const el = pointerScroller(event);
        if (el) reveal(el);
    };
    root.addEventListener('scroll', onScroll, { capture: true, passive: true });
    root.addEventListener('pointerover', onPointerOver as EventListener, { capture: true, passive: true });
    root.addEventListener('pointerdown', onPointerOver as EventListener, { capture: true, passive: true });
    root.addEventListener('pointermove', onPointerMove as EventListener, { capture: true, passive: true });
    root.addEventListener('pointerup', onPointerEnd as EventListener, { capture: true, passive: true });
    root.addEventListener('pointercancel', onPointerEnd as EventListener, { capture: true, passive: true });
    return () => {
        root.removeEventListener('scroll', onScroll, { capture: true });
        root.removeEventListener('pointerover', onPointerOver as EventListener, { capture: true });
        root.removeEventListener('pointerdown', onPointerOver as EventListener, { capture: true });
        root.removeEventListener('pointermove', onPointerMove as EventListener, { capture: true });
        root.removeEventListener('pointerup', onPointerEnd as EventListener, { capture: true });
        root.removeEventListener('pointercancel', onPointerEnd as EventListener, { capture: true });
        for (const [el, state] of active) {
            if (state.timer !== null) clearTimeout(state.timer);
            state.animation?.cancel();
            el.removeAttribute(SCROLLING_ATTR);
        }
        active.clear();
    };
}

/** The scroll container whose gutter a press lands on, and where: the pressed
 *  element or the nearest ancestor with a gutter under the point. A classic
 *  gutter lies outside every child's box, so the pressed element is the
 *  scroller itself; a bar that takes no room lies over the content, so the
 *  press lands on a child and the walk finds the scroller above it. A
 *  document's root is never one: its bars are the viewport's. */
function gutterPressed(event: MouseEvent): { el: HTMLElement; hit: NonNullable<ReturnType<typeof gutterAt>> } | null {
    const first = event.composedPath()[0];
    if (!(first instanceof HTMLElement)) return null;
    const doc = first.ownerDocument;
    for (let el: HTMLElement | null = first; el && el !== doc.documentElement && el !== doc.body; el = el.parentElement) {
        // A hidden scrollbar can still report the sheet's pseudo-element
        // thickness, even though its last pixels belong to content.
        if (getComputedStyle(el).scrollbarWidth === 'none') continue;
        const hit = gutterAt(metricsOf(el), event.clientX, event.clientY);
        if (hit) return { el, hit };
    }
    return null;
}

function scrollTo(el: HTMLElement, axis: Axis, value: number): void {
    el.scrollTo({ [axis === 'y' ? 'top' : 'left']: value, behavior: 'instant' } as ScrollToOptions);
}

/** Owns the thumb drag on every scroll container in `root`'s tree: a press on
 *  a thumb captures the pointer and moves the scroll with it until the
 *  release; a press on the track pages. The browser's own scrollbar press is
 *  cancelled. Returns the undo. */
export function installThumbDrag(root: Document | ShadowRoot = document): () => void {
    let drag: { el: HTMLElement; pointerId: number; gutter: Gutter; grab: number } | null = null;
    const end = () => {
        if (!drag) return;
        const { el, pointerId } = drag;
        drag = null;
        el.removeAttribute(DRAGGING_ATTR);
        try { el.releasePointerCapture(pointerId); } catch { /* already released */ }
    };
    const onPointerDown = (event: PointerEvent) => {
        if (event.button !== 0 || event.pointerType === 'touch') return;
        const pressed = gutterPressed(event);
        if (!pressed) return;
        const { el, hit } = pressed;
        event.preventDefault();
        const { gutter } = hit;
        if (!hit.onThumb) {
            const page = gutter.trackLength * PAGE_FRACTION;
            const next = gutter.scroll + (hit.along < gutter.thumbStart ? -page : page);
            scrollTo(el, gutter.axis, Math.min(gutter.maxScroll, Math.max(0, next)));
            return;
        }
        end();
        drag = { el, pointerId: event.pointerId, gutter, grab: hit.along - gutter.thumbStart };
        el.setAttribute(DRAGGING_ATTR, '');
        try { el.setPointerCapture(event.pointerId); } catch { /* unsupported (jsdom) */ }
    };
    const onMouseDown = (event: MouseEvent) => {
        // The compatibility press behind a cancelled pointerdown never comes;
        // this is for a mouse press that arrives on its own.
        if (gutterPressed(event)) event.preventDefault();
    };
    const onPointerMove = (event: PointerEvent) => {
        if (!drag || event.pointerId !== drag.pointerId) return;
        event.preventDefault();
        const along = drag.gutter.axis === 'y' ? event.clientY : event.clientX;
        scrollTo(drag.el, drag.gutter.axis, dragScroll(drag.gutter, along, drag.grab));
    };
    const onPointerEnd = (event: PointerEvent) => {
        if (drag && event.pointerId === drag.pointerId) end();
    };
    const listeners: Array<[string, EventListener]> = [
        ['pointerdown', onPointerDown as EventListener], ['mousedown', onMouseDown as EventListener],
        ['pointermove', onPointerMove as EventListener], ['pointerup', onPointerEnd as EventListener],
        ['pointercancel', onPointerEnd as EventListener],
    ];
    for (const [type, listener] of listeners) root.addEventListener(type, listener, { capture: true });
    return () => {
        end();
        for (const [type, listener] of listeners) root.removeEventListener(type, listener, { capture: true });
    };
}

/** Everything a tree needs for its scrollbars to behave: the activity mark
 *  and the thumb drag. Returns the undo. */
export function installScrollbars(root: Document | ShadowRoot = document): () => void {
    const undoActivity = installScrollActivity(root);
    const undoDrag = installThumbDrag(root);
    return () => { undoDrag(); undoActivity(); };
}

let sheet: CSSStyleSheet | null | undefined;

/** The sheet as one constructed stylesheet, shared by every tree that adopts
 *  it; null where the platform cannot construct one (jsdom). */
export function scrollbarSheet(): CSSStyleSheet | null {
    if (sheet !== undefined) return sheet;
    try {
        const made = new CSSStyleSheet();
        made.replaceSync(SCROLLBAR_CSS);
        sheet = made;
    } catch {
        sheet = null;
    }
    return sheet;
}

/** Gives a document or a native renderer's shadow root the workbench's
 *  scrollbars' look. */
export function adoptScrollbars(root: Document | ShadowRoot): void {
    const made = scrollbarSheet();
    if (!made || !('adoptedStyleSheets' in root)) return;
    if (!root.adoptedStyleSheets.includes(made)) root.adoptedStyleSheets = [...root.adoptedStyleSheets, made];
}
