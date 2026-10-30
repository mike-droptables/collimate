/**
 * Wheel focus: a vertical wheel over an unfocused cell focuses it, and the
 * content under the pointer still takes that wheel, animated from where it
 * stood as a native notch would be, with every notch of the same burst
 * folded into that one animation.
 *
 * An unfocused cell's renderer is hit-test-disabled behind a focus scrim
 * (NativeRenderer's shadow host and a pooled iframe are pointer-events:none
 * until the cell holds the canvas focus), so the wheel lands on the scrim,
 * whose scroll chain (the card, the scene) scrolls nothing. The card
 * selects on it (StackItem), the leaf under it activates (ContentPane), and
 * the scrims replay the swallowed travel into the scroller under the
 * pointer, the way NativeRenderer replays the first click. After the focus
 * flip the renderer takes wheels natively.
 */

/** One line of a deltaMode-1 wheel (a mouse notch on some platforms), in px. */
export const WHEEL_LINE_PX = 16;

/** The fields of a wheel event this module reads. */
export interface WheelLike {
    deltaX: number;
    deltaY: number;
    deltaMode?: number;
    ctrlKey?: boolean;
}

/** A wheel that focuses what it lands on: vertical-dominant and not a pinch.
 *  The exact complement of the canvas swipe (useSwipeNavigate), which owns
 *  the horizontal wheel, and of ctrl-wheel, the pinch. */
export function isFocusWheel(e: WheelLike): boolean {
    return !e.ctrlKey && Math.abs(e.deltaX) <= Math.abs(e.deltaY);
}

/** Whether `el` can still travel `delta` along `axis` (not at that edge). */
function canTravel(el: Element, axis: 'x' | 'y', delta: number): boolean {
    if (delta === 0) return false;
    const max = axis === 'y' ? el.scrollHeight - el.clientHeight : el.scrollWidth - el.clientWidth;
    if (max <= 1) return false;
    const pos = axis === 'y' ? el.scrollTop : el.scrollLeft;
    return delta > 0 ? pos < max - 1 : pos > 1;
}

const SCROLLING = new Set(['auto', 'scroll', 'overlay']);

/** `el`'s parent, out of a nested shadow root to its host; `root` is the end. */
function parentWithin(el: Element, root: Document | ShadowRoot): Element | null {
    if (el.parentElement) return el.parentElement;
    const node = el.getRootNode();
    return node !== root && node.nodeType === Node.DOCUMENT_FRAGMENT_NODE ? (node as ShadowRoot).host : null;
}

/** A scroll container that can still take travel (dx, dy): what a native
 *  wheel scrolls, walking out from the hit element (scroll chaining). */
function takesTravel(el: Element, dx: number, dy: number): boolean {
    const view = el.ownerDocument.defaultView;
    if (!view) return false;
    const style = view.getComputedStyle(el);
    return (SCROLLING.has(style.overflowY) && canTravel(el, 'y', dy))
        || (SCROLLING.has(style.overflowX) && canTravel(el, 'x', dx));
}

/**
 * The element a native wheel at viewport point (x, y) would scroll by
 * (dx, dy) inside `root`, or null when nothing there can. Hit-tests like
 * the browser (an element with pointer-events:none is skipped, which is
 * why NativeRenderer waits for its host to flip), descends into nested
 * shadow roots and same-origin frames, and walks out to the nearest
 * scroller still able to move that way, never past `root`.
 */
export function scrollTargetAt(root: Document | ShadowRoot, x: number, y: number, dx: number, dy: number): Element | null {
    let hit = root.elementFromPoint(x, y);
    // A shadow root answers with the light-DOM element on top when nothing
    // of its own is hit (a host still pointer-events:none): not ours, and
    // whatever scrolls there took the wheel natively already.
    if (!hit || !root.contains(hit)) return null;
    while (hit.shadowRoot) {
        const inner: Element | null = hit.shadowRoot.elementFromPoint(x, y);
        if (!inner || !hit.shadowRoot.contains(inner)) break;
        hit = inner;
    }
    if (hit.tagName === 'IFRAME') return scrollTargetInFrame(hit as HTMLIFrameElement, x, y, dx, dy);
    for (let el: Element | null = hit; el; el = parentWithin(el, root)) {
        if (takesTravel(el, dx, dy)) return el;
    }
    if (root.nodeType === Node.DOCUMENT_NODE) {
        const viewport = (root as Document).scrollingElement;
        if (viewport && (canTravel(viewport, 'y', dy) || canTravel(viewport, 'x', dx))) return viewport;
    }
    return null;
}

/** Whether viewport point (x, y) lies over a frame in `root` whose document
 *  the host cannot see (an opaque origin: a sandbox without
 *  allow-same-origin, like the html renderer's page). No replay reaches its
 *  scroller ({@link scrollTargetInFrame} is null there). Geometric, so it
 *  answers while the renderer is still hit-test-disabled. */
export function opaqueFrameAt(root: Document | ShadowRoot, x: number, y: number): boolean {
    for (const frame of Array.from(root.querySelectorAll('iframe'))) {
        const r = frame.getBoundingClientRect();
        if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) continue;
        let doc: Document | null = null;
        try { doc = frame.contentDocument; } catch { /* cross-origin */ }
        if (!doc) return true;
    }
    return false;
}

/** {@link scrollTargetAt} inside a frame, from viewport coordinates of the
 *  frame's own document. Null for an opaque-origin frame (a sandbox without
 *  allow-same-origin, like the html renderer's page), whose scroller the host
 *  cannot reach. The frame element's own pointer-events does not matter:
 *  hit-testing runs in the frame's document. */
export function scrollTargetInFrame(frame: HTMLIFrameElement, x: number, y: number, dx: number, dy: number): Element | null {
    let doc: Document | null;
    try { doc = frame.contentDocument; } catch { return null; }
    if (!doc) return null;
    const rect = frame.getBoundingClientRect();
    // A scaled frame (a transformed ancestor) maps viewport px to its own.
    const scale = frame.offsetWidth ? rect.width / frame.offsetWidth : 1;
    return scrollTargetAt(
        doc,
        (x - rect.left) / scale - frame.clientLeft,
        (y - rect.top) / scale - frame.clientTop,
        dx, dy,
    );
}

/** Where a replay scrolls: the scroller under viewport point (x, y) that
 *  can travel the (dx, dy) direction, or null. */
export type WheelTargetResolver = (x: number, y: number, dx: number, dy: number) => Element | null;

export interface WheelReplayOptions {
    /** The light-DOM element whose wheels belong to a burst before its
     *  scroller is known (the renderer's wrapper, which outlives the scrim).
     *  Once the scroller is known, wheels over it join as well. */
    within?: () => Element | null;
    /** The animation's frame source; requestAnimationFrame, injectable. */
    frame?: (run: (now: number) => void) => void;
}

export interface WheelReplay {
    (e: WheelLike & { clientX: number; clientY: number }): void;
    /** Whether a burst is in flight: wheels over it are being folded in. */
    active(): boolean;
    /** Drop the burst and its listeners (the scrim's detach). */
    cancel(): void;
}

/** The share of the remaining travel covered per 60Hz frame: about 95% in
 *  ten frames, the feel of the browser's own wheel animation. */
const FRAME_SHARE = 0.22;
const FRAME_MS = 1000 / 60;

/**
 * A scrim's wheel handler that replays the travel it swallowed into the
 * scroller `resolve` finds, and then OWNS the burst: the travel animates
 * from where the content stood, and every further focus wheel over the
 * same cell folds into that animation until it settles, after which wheels
 * land natively. `defer` says when the first replay runs: immediately for
 * a pooled iframe (its document hit-tests regardless of the element's
 * pointer-events), after the focus flip has painted for a shadow host
 * (elementFromPoint skips it while it is pointer-events:none; that scrim
 * also cancels the native scroll, {@link attachScrimWheel}).
 *
 * Owning the burst is what keeps every notch. The focus commit removes the
 * scrim and flips the host in one paint, so a second notch 50ms after the
 * first would otherwise scroll natively while the first is still waiting
 * to be replayed, and the browser lets the later scroll cancel the
 * earlier animation: one of them is lost. Here a capture listener on the
 * document (and on the scroller's own document, for a frame) cancels and
 * folds those notches instead (owner 2026-10-09). Only a focusing wheel is
 * replayed ({@link isFocusWheel}): a horizontal one is the canvas swipe's
 * and must not also scroll the cell, a ctrl one the pinch's.
 */
export function createWheelReplay(
    resolve: WheelTargetResolver,
    defer: (run: () => void) => void,
    opts: WheelReplayOptions = {},
): WheelReplay {
    const frame = opts.frame ?? ((run: (now: number) => void) => { requestAnimationFrame(run); });
    // px travel, plus page travel (deltaMode 2) sized by the target it lands on.
    let pending: { x: number; y: number; px: number; py: number; pagesX: number; pagesY: number } | null = null;
    let burst: { target: Element; goalX: number; goalY: number; last: number | null } | null = null;
    const listeners: Array<() => void> = [];
    const active = () => pending !== null || burst !== null;

    const listen = (doc: Document, scope: () => Element | null) => {
        const onWheel = (e: Event) => {
            const wheel = e as WheelEvent;
            if (!isFocusWheel(wheel)) return;
            const within = scope();
            if (!within || !wheel.composedPath().includes(within)) return;
            wheel.preventDefault();
            fold(wheel);
        };
        doc.addEventListener('wheel', onWheel, { capture: true, passive: false });
        listeners.push(() => doc.removeEventListener('wheel', onWheel, { capture: true }));
    };
    const release = () => {
        for (const off of listeners.splice(0)) off();
        burst = null;
    };

    const travel = (e: WheelLike, target: Element | null): { dx: number; dy: number } => {
        const unit = e.deltaMode === 1 ? WHEEL_LINE_PX : e.deltaMode === 2 ? 0 : 1;
        const pages = e.deltaMode === 2 && target ? { x: target.clientWidth, y: target.clientHeight } : { x: 0, y: 0 };
        return { dx: e.deltaX * unit + e.deltaX * pages.x, dy: e.deltaY * unit + e.deltaY * pages.y };
    };

    const aim = (dx: number, dy: number) => {
        const b = burst;
        if (!b) return;
        const { target } = b;
        b.goalX = Math.max(0, Math.min(target.scrollWidth - target.clientWidth, b.goalX + dx));
        b.goalY = Math.max(0, Math.min(target.scrollHeight - target.clientHeight, b.goalY + dy));
    };

    const step = (now: number) => {
        const b = burst;
        if (!b) return;
        const { target } = b;
        const dt = b.last === null ? FRAME_MS : Math.max(0, now - b.last);
        b.last = now;
        const share = 1 - Math.pow(1 - FRAME_SHARE, dt / FRAME_MS);
        const remainingX = b.goalX - target.scrollLeft;
        const remainingY = b.goalY - target.scrollTop;
        if (Math.abs(remainingX) < 0.5 && Math.abs(remainingY) < 0.5) {
            target.scrollLeft = b.goalX;
            target.scrollTop = b.goalY;
            release();
            return;
        }
        // At least a whole pixel a frame, capped at what remains: a scroller
        // that snaps to whole pixels would otherwise stall short of the goal
        // and the burst would never release.
        target.scrollLeft += stepBy(remainingX, share);
        target.scrollTop += stepBy(remainingY, share);
        frame(step);
    };

    const fold = (e: WheelLike & { clientX: number; clientY: number }) => {
        if (pending) {
            pending.x = e.clientX;
            pending.y = e.clientY;
            if (e.deltaMode === 2) {
                pending.pagesX += e.deltaX;
                pending.pagesY += e.deltaY;
            } else {
                const { dx, dy } = travel(e, null);
                pending.px += dx;
                pending.py += dy;
            }
            return;
        }
        if (burst) {
            const { dx, dy } = travel(e, burst.target);
            aim(dx, dy);
        }
    };

    const handler = (e: WheelLike & { clientX: number; clientY: number }) => {
        if (!isFocusWheel(e)) return;
        if (active()) { fold(e); return; }
        pending = { x: e.clientX, y: e.clientY, px: 0, py: 0, pagesX: 0, pagesY: 0 };
        fold(e);
        // Notches between now and the replay land on the wrapper (the scrim
        // is gone at the focus commit): fold them from the document.
        const within = opts.within?.();
        if (within) listen(within.ownerDocument, () => opts.within?.() ?? null);
        defer(() => {
            const p = pending;
            pending = null;
            if (!p) return;
            const target = resolve(p.x, p.y, p.px + p.pagesX, p.py + p.pagesY);
            if (!target) { release(); return; }
            burst = { target, goalX: target.scrollLeft, goalY: target.scrollTop, last: null };
            if (target.ownerDocument !== within?.ownerDocument) listen(target.ownerDocument, () => target);
            aim(p.px + p.pagesX * target.clientWidth, p.py + p.pagesY * target.clientHeight);
            frame(step);
        });
    };
    return Object.assign(handler, { active, cancel: () => { pending = null; release(); } });
}

/** One frame's eased step toward a remaining distance: the share of it,
 *  at least a whole pixel, never past it. */
function stepBy(remaining: number, share: number): number {
    if (remaining === 0) return 0;
    return Math.sign(remaining) * Math.min(Math.abs(remaining), Math.max(1, Math.abs(remaining * share)));
}

/**
 * A shadow host's focus scrim: swallow each focus wheel and replay it into
 * the scroller under the pointer once the host has flipped (`defer`).
 * Returns the detach (a callback ref's cleanup).
 *
 * Swallow, because the scrim's scroll chain scrolls nothing only while the
 * scrim is there. A non-passive listener on the wheel's path (the canvas
 * swipe's, this one) makes the wheel blocking, and Chromium then hit-tests
 * its native scroll only after the page handled it — by when the selection
 * commit has removed the scrim and the host takes pointer events, so the
 * shadow scroller took the wheel natively AND through the replay (the first
 * notch scrolled twice). Cancelled, the replay is the wheel's one delivery.
 * The listener is native and non-passive: React's onWheel is passive and
 * cannot cancel. Horizontal and ctrl wheels pass untouched (the swipe's and
 * the pinch's), and so does a wheel over an opaque frame
 * ({@link opaqueFrameAt}): no replay can reach that scroller, so the native
 * scroll keeps its chance to land once the host flips.
 */
export function attachScrimWheel(
    scrim: Element,
    root: () => Document | ShadowRoot | null,
    defer: (run: () => void) => void,
): () => void {
    // The burst's scope outlives the scrim: its parent, the renderer's
    // wrapper, which the flipped host and the next notches sit in. So does
    // the replay itself: the scrim detaches in the same focus commit the
    // wheel causes, before the deferred replay runs, so the detach removes
    // only the listener and never cancels the pending travel.
    const within = scrim.parentElement;
    const replay = createWheelReplay((x, y, dx, dy) => {
        const r = root();
        return r ? scrollTargetAt(r, x, y, dx, dy) : null;
    }, defer, { within: () => within });
    const onWheel = (e: Event) => {
        const wheel = e as WheelEvent;
        if (!isFocusWheel(wheel)) return;
        // A burst in flight already folded this wheel from the document.
        if (replay.active()) return;
        const r = root();
        if (r && opaqueFrameAt(r, wheel.clientX, wheel.clientY)) return;
        wheel.preventDefault();
        replay(wheel);
    };
    scrim.addEventListener('wheel', onWheel, { passive: false });
    return () => { scrim.removeEventListener('wheel', onWheel); };
}
